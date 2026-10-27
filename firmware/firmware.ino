#include <esp_task_wdt.h>
#include <driver/gpio.h>
#include <Arduino.h>
#include <stdarg.h>
#include <stddef.h>
#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <SPIFFS.h>
#include <RTClib.h> // DateTime/calendar only; MCP7940N registers are handled below.
#include <Adafruit_SHT4x.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <Preferences.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <vector>
#include <atomic>
#include <esp_random.h>
#include "Board.h"
#include "Safety.h"
#include "Telemetry.h"
#include "DashCA.h"
#include "SafetyTests.h"
#include "Ina238.h"
#include "LiteControl.h"

// В RTClib у DateTime есть свой конструктор копирования, но нет оператора присваивания,
// поэтому GCC при -Wextra предупреждает о каждом присваивании DateTime в этом файле. Класс
// хранит только целые поля даты и времени: неявное присваивание копирует их так же, как
// конструктор, правка библиотеки не нужна.
#pragma GCC diagnostic ignored "-Wdeprecated-copy"

// --- Настройки подключения к домашнему роутеру ---
// Значения по умолчанию используются только при первой прошивке / если NVS пуст.
// Реальные значения хранятся в Preferences и редактируются на вкладке "WiFi".
#define DEFAULT_WIFI_SSID ""
#define DEFAULT_WIFI_PASS ""
#define DEFAULT_AP_SSID       "MicroGarden_V1"
#define DEFAULT_AP_PASS ""
// Панель телеметрии (dashboard/). Пустой токен — отправка выключена.
#define DEFAULT_TELEMETRY_URL   "https://dash.dvinsk.lat/api/ingest"
#define DEFAULT_TELEMETRY_TOKEN ""
#define DEFAULT_DEVICE_LABEL    "microgarden-v1"
#define FIRMWARE_ID             "lite-1"

String admin_password; // Immutable after setup, never returned through HTTP.
String csrf_token;
static bool sd_online = false;
static bool led_supply_allowed = false;
static bool rgb_ready = false;

// MicroGarden_V1 is the only supported board. GPIOs and PWM timing are defined in Board.h.
#if !defined(CONFIG_IDF_TARGET_ESP32)
#error "MicroGarden_V1 requires ESP32-WROOM-32E-N4 (classic ESP32, 4 MB flash)"
#endif

// --- Ограничения длины строковых настроек ---
#define WIFI_SSID_MAX 32   // стандарт 802.11
#define WIFI_PASS_MAX 63   // WPA2

// =====================================================================================
// ДАННЫЕ ПРОШИВКИ: НАСТРОЙКИ И СОСТОЯНИЕ
// =====================================================================================
// Раньше это были ~50 отдельных глобальных переменных, которые три задачи на двух ядрах
// (loop, веб-сервер, задача Wi-Fi) читали и писали кто как, — отсюда была большая часть гонок.
// Теперь правило одно:
//   * Settings — то, что задал пользователь. Меняет только обработчик /save-settings:
//     разбирает форму в свою копию и публикует её целиком (publishSettings()).
//     Все остальные работают со своей копией (settingsSnapshot()).
//   * State — то, что происходит сейчас. Пишет только loop() в рабочую копию `state`
//     и публикует её (publishState()); веб читает stateSnapshot().
// Обе структуры — простые данные без String и указателей, поэтому копируются одним memcpy
// под спинлоком: без аллокаций, без таймаутов и без 503 на ровном месте.
//
// Типы объявлены до первой функции файла намеренно: Arduino IDE вставляет свои
// автоматические прототипы функций именно туда, и типы из их сигнатур должны быть известны.

struct Settings {
  // Профиль управления: 0 = выращивание, 1 = сушка
  int operation_mode;

  // Климат
  float temp_target;        // Дневная цель: лампа и вентиляторы по температуре
  float temp_delta;         // Ширина температурного диапазона и порог включения обогрева в Growing, °C
  float temp_target_night;  // Ночная цель для обогрева и вентиляторов, °C

  // Отдельный климатический профиль сушки
  float drying_temp_target;
  float drying_temp_delta;
  float drying_hum_target;
  float drying_hum_band;

  // Свет
  int led_on_hour, led_on_minute;
  int led_off_hour, led_off_minute;
  int led_min_limit, led_max_limit;              // Мощность лампы днём, %

  // Вентиляция
  int fan1_min_limit, fan1_max_limit;            // Скорость по температуре, %
  int fan2_min_limit, fan2_max_limit;

  // Обогрев: 0 = только день, 1 = только ночь, 2 = всегда, 3 = никогда
  int heater_mode;

  // Поправка показаний тока INA238, %: шунт 1% и усилитель дают ±3–5% без калибровки
  float power_cal_pct;

  // RGB-индикатор состояния платы
  int rgb_brightness;           // %, 0 = выключен
  int rgb_night_mode;           // 0 = как днём, 1 = ночью только неисправности, 2 = ночью выключен

  // Автополив
  uint8_t watering_days;       // Битовая маска: бит0=Пн, бит1=Вт, ... бит6=Вс
  int watering_hour, watering_minute;
  int watering_duration_sec;
  bool water_sensor_enabled;   // Учитывать ли датчик уровня воды перед стартом/во время полива

  uint32_t start_timestamp;    // Начало цикла выращивания (unixtime), 0 = цикл не начат

  // Сеть и облако
  char wifi_ssid[WIFI_SSID_MAX + 1];
  char wifi_pass[WIFI_PASS_MAX + 1];
  char ap_ssid[WIFI_SSID_MAX + 1];   // Точка доступа платы — к ней подключается телефон/ноутбук
  char ap_pass[WIFI_PASS_MAX + 1];   // WPA2 only; empty form means keep existing password

  // Отправка на сервер панели телеметрии
  char telemetry_url[TELEMETRY_URL_MAX + 1];
  char telemetry_token[TELEMETRY_TOKEN_MAX + 1];  // пусто = отправка выключена; наружу не отдаётся
  char device_label[DEVICE_LABEL_MAX + 1];
};

struct State {
  float temp;           // Последнее валидное показание SHT4x, °C
  float hum;            // ...и влажности, %
  int led_pwm;          // Текущий ШИМ, 0-255
  int fan1_pwm;
  int fan2_pwm;
  bool led_supply_blocked; // Missing/invalid INA238 sample or supply outside LED enable range.
  bool is_day;
  bool sht_online;
  bool rtc_online;
  bool clock_trusted;      // RTC отвечает и время в нём достоверно
  bool clock_usable;       // clock_trusted, либо программный таймер выставлен из RTC или вручную с момента запуска
  bool pump_active;
  bool heater_active;
  uint32_t heater_on_s;    // Сколько секунд нагреватель проработал за сутки платы (с полуночи или с запуска)
  uint8_t rtc_status;      // RtcStatus of the last RTC read
  bool water_present;      // Debounced float switch, whatever water_sensor_enabled says
  uint32_t last_water_day; // unixtime / 86400 of the day marked as watered, 0 = never
  uint32_t last_skip_day;  // Same day code when that mark was set but the pump did NOT run (RAM only)
  // INA238 power meter. The supply safety latch also depends on fresh bus voltage.
  bool power_ok;
  float bus_v;
  float current_a;
  float power_w;
  float energy_wh;         // Since midnight (or since boot, if the board restarted today)
};

enum RtcStatus : uint8_t { RTC_OK = 0, RTC_NOT_SET = 1, RTC_NO_RESPONSE = 2 };
enum TimeSetResult : uint8_t { TIME_SET_NONE = 0, TIME_SET_PENDING = 1, TIME_SET_OK = 2, TIME_SET_FAILED = 3 };

static const int OP_MODE_GROWING = 0;
static const int OP_MODE_DRYING = 1;

// Описание одной настройки для таблицы SETTING_DEFS (см. ниже)
enum SettingType : uint8_t { ST_FLOAT, ST_INT, ST_U8, ST_BOOL, ST_U32, ST_STRING };

enum SettingFlags : uint8_t {
  SF_SECRET  = 1 << 0,  // Наружу не отдаётся (только <param>_set), пустое поле формы = "не менять"
  SF_TRIM    = 1 << 1,  // Обрезать пробелы по краям
  SF_NO_FORM = 1 << 2,  // Из формы автоматически не разбирается (для поля есть особый обработчик)
};

struct SettingDef {
  const char *param;     // Имя поля в форме и в JSON /api/settings
  const char *nvsKey;    // Ключ в NVS (до 15 символов!) — исторически не везде совпадает с param
  SettingType type;
  uint16_t offset;       // Где поле лежит внутри Settings
  uint16_t size;         // Его размер (для строк — размер буфера)
  float lo, hi;          // Числа: диапазон клэмпа. Строки: допустимая длина непустого значения
  float def;             // Значение по умолчанию для чисел
  const char *defStr;    // ...и для строк
  uint8_t flags;
  const char *errGroup;  // Строки: какой баннер ошибки показать на странице, если значение отклонено
};

// Рабочая копия состояния. Трогает ТОЛЬКО loop() (и setup() до старта остальных задач)
State state = {}; // setup sets the initial LED supply block before publishing.

// Опубликованные копии для остальных задач — доступ только через функции ниже
static State stateShared = {};
static portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
static Settings settingsShared = {};
static portMUX_TYPE settingsMux = portMUX_INITIALIZER_UNLOCKED;

// --- Кольцевой буфер для показа Serial.print()-сообщений на веб-странице (/api/console) ---
#define LOG_BUFFER_SIZE 4096
static char logBuffer[LOG_BUFFER_SIZE];
static volatile size_t logHead = 0;
static volatile bool logWrapped = false;
// Сколько байт журнала записано с момента запуска (не обнуляется при переполнении кольца).
// По нему сервер панели отличает новое от уже принятого и замечает потерянные байты.
static volatile uint32_t logTotal = 0;
static portMUX_TYPE logMux = portMUX_INITIALIZER_UNLOCKED;

void logRaw(const char *data, size_t len) {
  portENTER_CRITICAL(&logMux);
  for (size_t i = 0; i < len; i++) {
    logBuffer[logHead] = data[i];
    logHead = (logHead + 1) % LOG_BUFFER_SIZE;
    if (logHead == 0) logWrapped = true;
  }
  logTotal = logTotal + (uint32_t)len;
  portEXIT_CRITICAL(&logMux);
}

// Копия хранящейся части журнала для задачи телеметрии, по порядку записи. Возвращает длину
// и позицию первого байта в потоке с момента запуска. Под спинлоком только memcpy, как в
// getLogSnapshot().
static size_t logCopyWindow(char *dst, uint32_t &windowStart) {
  portENTER_CRITICAL(&logMux);
  const size_t head = logHead;
  size_t len;
  if (!logWrapped) {
    memcpy(dst, logBuffer, head);
    len = head;
  } else {
    const size_t tailLen = LOG_BUFFER_SIZE - head;
    memcpy(dst, logBuffer + head, tailLen);
    memcpy(dst + tailLen, logBuffer, head);
    len = LOG_BUFFER_SIZE;
  }
  windowStart = logTotal - (uint32_t)len;
  portEXIT_CRITICAL(&logMux);
  return len;
}

// Замена logPrintln()/logPrintf() — пишет и на аппаратный UART, и в буфер для веба.
// Core 0 (задача Wi-Fi) и Core 1 (loop/setup) могут писать одновременно, поэтому буфер
// защищён спинлоком (portENTER_CRITICAL), а не мьютексом — запись короткая и частая.
void logPrintln(const String &msg) {
  Serial.println(msg);
  String line = msg + "\n";
  logRaw(line.c_str(), line.length());
}

void logPrintf(const char *fmt, ...) {
  char buf[192];
  va_list args;
  va_start(args, fmt);
  const int written = vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  size_t len = written < 0 ? 0 : (size_t)written;
  if (len >= sizeof(buf)) {
    // Строка не поместилась. Режем так, чтобы не оставить обрывок русской буквы (UTF-8 из двух
    // байт) и вернуть потерянный перевод строки: иначе обрывок попал бы в журнал и в запрос
    // телеметрии, а следующая строка приклеилась бы к этой.
    len = sizeof(buf) - 2;
    while (len > 0 && ((uint8_t)buf[len] & 0xC0) == 0x80) --len;
    buf[len++] = '\n';
    buf[len] = '\0';
  }
  Serial.print(buf);
  logRaw(buf, len);
}

// Возвращает содержимое буфера в правильном хронологическом порядке одной строкой.
// Под спинлоком делаем ТОЛЬКО memcpy: собирать String (а значит, дёргать кучу) с
// выключенными прерываниями нельзя — на 4 КБ это тысячи реаллокаций и реальный риск
// паники/срыва watchdog, а страница дёргает /api/console каждые 3 секунды.
// Буфер статический (4 КБ на стеке async-задачи не поместятся); обработчики
// AsyncWebServer выполняются последовательно в одной задаче, поэтому гонки за него нет.
String getLogSnapshot() {
  static char snapshotCopy[LOG_BUFFER_SIZE + 1];
  size_t len;

  portENTER_CRITICAL(&logMux);
  size_t head = logHead;
  bool wrapped = logWrapped;
  if (!wrapped) {
    memcpy(snapshotCopy, logBuffer, head);
    len = head;
  } else {
    size_t tailLen = LOG_BUFFER_SIZE - head;
    memcpy(snapshotCopy, logBuffer + head, tailLen);
    memcpy(snapshotCopy + tailLen, logBuffer, head);
    len = LOG_BUFFER_SIZE;
  }
  portEXIT_CRITICAL(&logMux);

  snapshotCopy[len] = 0;
  return String(snapshotCopy);
}

// Экранирование строки для безопасной вставки в JSON: имена файлов SD, а также SSID,
// имя устройства и прочее, что вводит пользователь (одна кавычка ломала разбор на странице)
String jsonEscape(const char* s) {
  String out;
  for (int i = 0; s[i] != '\0'; i++) {
    char c = s[i];
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if (c == '\n' || c == '\r' || c == '\t') { out += ' '; }
    else if ((uint8_t)c < 0x20) { /* прочие control-символы пропускаем */ }
    else out += c;
  }
  return out;
}

// uint64_t -> String: обычный String(uint64_t) в Arduino не поддерживается,
// а SD.totalBytes()/usedBytes() возвращают именно uint64_t
String u64str(uint64_t v) {
  if (v == 0) return "0";
  char buf[21];
  int i = 20;
  buf[i] = '\0';
  while (v > 0 && i > 0) { buf[--i] = '0' + (char)(v % 10); v /= 10; }
  return String(&buf[i]);
}

// Удаляет из корня SD только файлы логов (*.csv). Раньше здесь был рекурсивный
// wipePath("/"), который сносил вообще всё содержимое карты — включая файлы, не имеющие
// к логам отношения. Сначала собираем список, потом удаляем: удалять во время обхода
// каталога нельзя, это ломает итератор FatFS.
int clearCsvLogs() {
  int removed = 0;
  std::vector<String> victims;

  File dir = SD.open("/");
  if (!dir) return 0;
  if (!dir.isDirectory()) { dir.close(); return 0; }

  File entry = dir.openNextFile();
  while (entry) {
    if (!entry.isDirectory()) {
      String full = String(entry.path()); // абсолютный путь (ESP32 core 3.x)
      String lower = full;
      lower.toLowerCase();
      if (lower.endsWith(".csv")) victims.push_back(full);
    }
    entry.close();
    entry = dir.openNextFile();
  }
  dir.close();

  for (size_t i = 0; i < victims.size(); i++) {
    if (SD.remove(victims[i])) removed++;
  }
  return removed;
}

// --- Внутреннее состояние loop(): никто, кроме loop() и setup(), эти переменные не трогает ---
uint32_t pump_start_ms = 0;   // Отсчёт длительности полива по millis(), а не по RTC
uint32_t pump_duration_ms = 0;
uint32_t last_watering_day = 0;    // Защита от повторного срабатывания в тот же день (хранится в NVS)

// Полив, пропущенный из-за отключения питания или перезагрузки, догоняется в течение этого
// окна. Позже — уже нет: полив среди ночи вместо утреннего хуже, чем пропуск одного дня.
const uint32_t WATERING_CATCHUP_SEC = 4UL * 3600;
// Момент (unixtime), с которого действует расписание полива: когда пользователь последний раз
// менял расписание, режим или часы. Поливы, запланированные раньше, не догоняются — иначе
// сохранение настроек днём сразу запускало бы насос за утреннее время. После перезагрузки
// равен 0: тогда пропуск вызван простоем платы, и его как раз нужно догнать.
uint32_t watering_armed_from = 0;

// Датчик уровня воды с антидребезгом: "воды нет" только после WATER_DEBOUNCE_MS непрерывно.
// Раньше хватало одного чтения, и колыхание воды при работающем насосе обрывало полив.
const uint32_t WATER_DEBOUNCE_MS = 2000;
Debounce water_level = {true, true, 0};

unsigned long last_sht_retry = 0;                  // Когда последний раз пытались восстановить датчик
const unsigned long SHT_RETRY_INTERVAL_MS = 30000; // Пауза между попытками восстановления, мс
int sht_recovery_streak = 0;                       // Счётчик подряд успешных попыток восстановления
unsigned long last_emergency_log_ms = 0;           // Когда последний раз писали в консоль про аварийный режим (0 = ещё не писали)
const int SHT_RECOVERY_STREAK_NEEDED = 3;          // Сколько подряд удачных попыток нужно, чтобы снова доверять датчику
unsigned long last_rtc_retry = 0;                  // Когда последний раз пытались восстановить RTC
const unsigned long RTC_RETRY_INTERVAL_MS = 30000; // Пауза между попытками восстановления, мс
int rtc_recovery_streak = 0;                       // Счётчик подряд успешных попыток восстановления
const int RTC_RECOVERY_STREAK_NEEDED = 3;          // Сколько подряд удачных попыток нужно, чтобы снова доверять RTC

// Переменные для программного дублирования времени (на случай отказа RTC)
uint32_t backup_unixtime = 1774838400; // Дефолтный 2026 год, если RTC умер сразу при старте
uint32_t last_rtc_check_ms = 0;
// Программный таймер можно считать временем, только если он был выставлен настоящим источником
// (чтение RTC или ввод времени владельцем) с момента запуска. Дефолтная дата выше источником не
// является: пока флаг не поднят, полив и свет без измерителя питания по таймеру не работают.
static bool backup_clock_synced = false;

// Время и запросы к RTC трогает только loop(). Веб-обработчики крутятся в задаче
// AsyncWebServer (другое ядро): раньше они звали getSafeDateTime() и rtcAdjust() напрямую,
// то есть лезли в I2C параллельно с опросом SHT4x и одновременно правили rtc_online/backup_unixtime.
std::atomic<uint32_t> cached_unixtime{0};  // Снимок времени для веб-обработчиков, обновляет loop()
std::atomic<uint32_t> pending_time_set{0}; // Запрос "выставить часы" с формы; применяет loop()
std::atomic<uint8_t> time_set_result{TIME_SET_NONE}; // Итог последней установки часов для страницы настроек

// --- Хендлы FreeRTOS для многоядерности ---
TaskHandle_t WifiTaskHandle = NULL;
TaskHandle_t TelemetryTaskHandle = NULL;
static char boot_id[9] = ""; // Случайный номер запуска: по нему сервер отличает перезагрузку от повтора порции

// --- Инициализация объектов ---
Adafruit_SHT4x sht40 = Adafruit_SHT4x();
bool rtc_present = false; // MCP7940N at 0x6f; presence alone never makes time trusted.
AsyncWebServer server(80);
Preferences preferences;

// RGB channels share the 1 kHz nDIM timer, never the 25 kHz fan timer.
void setStatusColor(StatusColor color, uint8_t duty = RGB_DUTY) {
  if (!rgb_ready) return;
  // Каждый канал только включён или выключен — промежуточных яркостей больше нет,
  // поэтому цвета не уползают от разброса светодиодов и видны при ярком свете.
  const bool red = color == StatusColor::Red || color == StatusColor::Purple ||
                   color == StatusColor::Yellow;
  const bool green = color == StatusColor::Green || color == StatusColor::Yellow;
  const bool blue = color == StatusColor::Blue || color == StatusColor::Purple;
  ledcWrite(RGB_RED_PIN, red ? duty : 0);
  ledcWrite(RGB_GREEN_PIN, green ? duty : 0);
  ledcWrite(RGB_BLUE_PIN, blue ? duty : 0);
}

// Яркость и ночной режим берутся из настроек (страница «Общее»). Ночью — вне расписания света.
void updateStatusLed(const Settings &cfg) {
  const StatusColor color = statusColor(state.sht_online, state.clock_trusted,
                                        state.power_ok && state.led_supply_blocked,
                                        sd_online, state.power_ok);
  setStatusColor(rgbVisibleColor(color, state.is_day, cfg.rgb_night_mode),
                 rgbDutyForPercent(cfg.rgb_brightness));
}

void stopPumpAtDeadline() {
  if (state.pump_active && elapsedMs(millis(), pump_start_ms) >= pump_duration_ms) {
    state.pump_active = false;
    digitalWrite(PUMP_PIN, LOW);
    logPrintln("[Watering] Pump stopped at deadline.");
  }
}

// --- Хелперы валидации входных данных (защита от некорректных значений из веб-формы) ---
int clampInt(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

// "YYYY-MM-DD" -> компоненты. Раньше даты брались из формы как есть: короткая или
// битая строка давала substring("") -> toInt() == 0, и в RTC улетало 0000-00-00.
bool parseDate(const String &str, int &y, int &m, int &d) {
  if (str.length() < 10) return false;
  for (int i = 0; i < 10; i++) {
    char c = str[i];
    if (i == 4 || i == 7) { if (c != '-') return false; }
    else if (c < '0' || c > '9') return false;
  }
  y = str.substring(0, 4).toInt();
  m = str.substring(5, 7).toInt();
  d = str.substring(8, 10).toInt();
  return validCalendar(y, m, d, 0, 0, 0);
}

// "YYYY-MM-DDTHH:MM" или "YYYY-MM-DDTHH:MM:SS" — оба варианта отдаёт <input type="datetime-local">.
// Секунды нужны кнопке синхронизации: без них часы платы отставали до минуты.
bool parseDateTimeLocal(const String &str, DateTime &out) {
  int y, m, d;
  if (!parseDate(str, y, m, d)) return false;
  const bool withSeconds = str.length() == 19;
  if ((str.length() != 16 && !withSeconds) || str[10] != 'T' || str[13] != ':') return false;
  if (withSeconds && str[16] != ':') return false;
  for (int i = 11; i < (int)str.length(); i++) {
    if (i == 13 || i == 16) continue;
    if (str[i] < '0' || str[i] > '9') return false;
  }
  int hh = str.substring(11, 13).toInt();
  int mm = str.substring(14, 16).toInt();
  int ss = withSeconds ? str.substring(17, 19).toInt() : 0;
  if (hh > 23 || mm > 59 || ss > 59) return false;
  out = DateTime(y, m, d, hh, mm, ss);
  return true;
}

// Имя файла лога собирается из параметра ?date=, поэтому его форма проверяется строго:
// ровно YYYY-MM-DD. Иначе запрос вида ?date=../../secret уводил чтение за пределы корня.
bool isValidLogDate(const String &str) {
  int y, m, d;
  return str.length() == 10 && parseDate(str, y, m, d);
}

// =====================================================================================
// ОБМЕН ДАННЫМИ МЕЖДУ ЗАДАЧАМИ
// =====================================================================================
// Спинлок, а не мьютекс: под ним только memcpy (~40 байт State, ~500 байт Settings —
// единицы микросекунд), поэтому захват не может "не успеть", и читателям не нужен
// запасной путь на случай таймаута.

State stateSnapshot() {
  State s;
  portENTER_CRITICAL(&stateMux);
  memcpy(&s, &stateShared, sizeof(State));
  portEXIT_CRITICAL(&stateMux);
  return s;
}

// Публикует рабочую копию loop() для остальных задач, если она изменилась. Вызывается
// в конце каждой итерации loop(): сравнить 40 байт дешевле, чем помнить, где именно
// состояние поменялось. Читать stateShared без спинлока здесь можно — кроме этой
// функции, его никто не пишет.
void publishState() {
  if (memcmp(&state, &stateShared, sizeof(State)) == 0) return;
  portENTER_CRITICAL(&stateMux);
  memcpy(&stateShared, &state, sizeof(State));
  portEXIT_CRITICAL(&stateMux);
}

Settings settingsSnapshot() {
  Settings s;
  portENTER_CRITICAL(&settingsMux);
  memcpy(&s, &settingsShared, sizeof(Settings));
  portEXIT_CRITICAL(&settingsMux);
  return s;
}

void publishSettings(const Settings &s) {
  portENTER_CRITICAL(&settingsMux);
  memcpy(&settingsShared, &s, sizeof(Settings));
  portEXIT_CRITICAL(&settingsMux);
}

// =====================================================================================
// ТАБЛИЦА НАСТРОЕК
// =====================================================================================
// Единственное место, где описана каждая настройка: имя в форме/JSON, ключ NVS, диапазон
// и значение по умолчанию. Из неё работают загрузка из NVS, разбор формы, сохранение и
// выдача в /api/settings. Раньше каждое поле было выписано руками в трёх местах, и они
// расходились (так "терялась" temp_target_night, так разъехались дефолты led_off_hour).
//
// Чтобы добавить настройку: поле в struct Settings + одна строка здесь. Всё.
//
// Ключи NVS менять нельзя: по ним уже прошитые платы читают сохранённые значения.

// Проверка типов на этапе компиляции. Эти функции только объявлены и используются
// исключительно внутри sizeof(), то есть никогда не вызываются. Если тип в строке таблицы
// не совпадает с типом поля (скажем, SET_INT для uint8_t), сборка упадёт с ошибкой —
// вместо того чтобы молча записать 4 байта в однобайтовое поле.
char requireFloat(float *);
char requireInt(int *);
char requireU8(uint8_t *);
char requireBool(bool *);
char requireU32(uint32_t *);
char requireChars(char *);

#define S_PTR(field) (&((Settings *)0)->field)
#define S_LOC(field) (uint16_t)offsetof(Settings, field), (uint16_t)sizeof(((Settings *)0)->field)

#define SET_FLOAT(param, key, field, lo, hi, def) \
  { param, key, ST_FLOAT, S_LOC(field) + 0 * sizeof(requireFloat(S_PTR(field))), lo, hi, def, nullptr, 0, nullptr }
#define SET_INT(param, key, field, lo, hi, def) \
  { param, key, ST_INT, S_LOC(field) + 0 * sizeof(requireInt(S_PTR(field))), lo, hi, def, nullptr, 0, nullptr }
#define SET_U8(param, key, field, lo, hi, def) \
  { param, key, ST_U8, S_LOC(field) + 0 * sizeof(requireU8(S_PTR(field))), lo, hi, def, nullptr, 0, nullptr }
#define SET_BOOL(param, key, field, def) \
  { param, key, ST_BOOL, S_LOC(field) + 0 * sizeof(requireBool(S_PTR(field))), 0, 1, (def) ? 1 : 0, nullptr, 0, nullptr }
#define SET_U32_NOFORM(param, key, field) \
  { param, key, ST_U32, S_LOC(field) + 0 * sizeof(requireU32(S_PTR(field))), 0, 0, 0, nullptr, SF_NO_FORM, nullptr }
// Для строк заодно проверяется, что максимальная длина влезает в буфер поля
#define SET_STR(param, key, field, minLen, maxLen, def, flags, errGroup) \
  { param, key, ST_STRING, S_LOC(field) + 0 * sizeof(requireChars(((Settings *)0)->field)) \
      + 0 * sizeof(char[(maxLen) < sizeof(((Settings *)0)->field) ? 1 : -1]), \
    minLen, maxLen, 0, def, flags, errGroup }

static const SettingDef SETTING_DEFS[] = {
  //        param (форма/JSON)     ключ NVS           поле                   мин   макс   по умолч.
  // --- Профиль управления ---
  SET_INT  ("operation_mode",      "op_mode",         operation_mode,        OP_MODE_GROWING, OP_MODE_DRYING, OP_MODE_GROWING),
  // --- Климат ---
  SET_FLOAT("temp_target",         "temp_target",     temp_target,           0,    50,    25),
  SET_FLOAT("temp_delta",          "temp_delta",      temp_delta,            0.1,  20,    2),
  SET_FLOAT("temp_target_night",   "temp_night",      temp_target_night,     0,    50,    20),
  SET_FLOAT("drying_temp_target",  "dry_temp",        drying_temp_target,    0,    50,    18),
  SET_FLOAT("drying_temp_delta",   "dry_t_delta",     drying_temp_delta,     0.1,  20,    0.5),
  SET_FLOAT("drying_hum_target",   "dry_hum",         drying_hum_target,     0,    100,   55),
  SET_FLOAT("drying_hum_band",     "dry_h_band",      drying_hum_band,       0.1,  25,    5),
  // --- Свет ---
  SET_INT  ("led_on_hour",         "led_on_hour",     led_on_hour,           0,    23,    6),
  SET_INT  ("led_on_minute",       "led_on_minute",   led_on_minute,         0,    59,    0),
  SET_INT  ("led_off_hour",        "led_off_hour",    led_off_hour,          0,    23,    18),
  SET_INT  ("led_off_minute",      "led_off_minute",  led_off_minute,        0,    59,    0),
  SET_INT  ("led_min_limit",       "led_min_limit",   led_min_limit,         0,    100,   10),
  SET_INT  ("led_max_limit",       "led_max_limit",   led_max_limit,         0,    100,   100),
  // --- Вентиляция ---
  SET_INT  ("fan1_min_limit",      "fan1_min_limit",  fan1_min_limit,        0,    100,   20),
  SET_INT  ("fan1_max_limit",      "fan1_max_limit",  fan1_max_limit,        0,    100,   100),
  SET_INT  ("fan2_min_limit",      "fan2_min_limit",  fan2_min_limit,        0,    100,   20),
  SET_INT  ("fan2_max_limit",      "fan2_max_limit",  fan2_max_limit,        0,    100,   100),
  // --- Обогрев ---
  SET_INT  ("heater_mode",         "heater_mode",     heater_mode,           0,    3,     3),
  // --- Измерение мощности (INA238) ---
  SET_FLOAT("power_cal_pct",       "pwr_cal_pct",     power_cal_pct,         -20,  20,    0),
  // --- RGB-индикатор ---
  SET_INT  ("rgb_brightness",      "rgb_bright",      rgb_brightness,        0,    100,   RGB_BRIGHTNESS_DEFAULT_PCT),
  SET_INT  ("rgb_night_mode",      "rgb_night",       rgb_night_mode,        RGB_NIGHT_SAME, RGB_NIGHT_OFF, RGB_NIGHT_SAME),
  // --- Автополив ---
  SET_U8   ("watering_days",       "watering_days",   watering_days,         0,    127,   0),
  SET_INT  ("watering_hour",       "watering_hour",   watering_hour,         0,    23,    8),
  SET_INT  ("watering_minute",     "watering_minute", watering_minute,       0,    59,    0),
  SET_INT  ("watering_duration",   "watering_dur",    watering_duration_sec, 1,    3600,  30),
  SET_BOOL ("water_sensor_enabled","water_sensor",    water_sensor_enabled,               false),
  // --- Цикл выращивания: из формы приходит строкой start_date, разбирается в /save-settings ---
  SET_U32_NOFORM("start_time",     "start_time",      start_timestamp),
  // --- Сеть. Мин./макс. — допустимая длина непустого значения ---
  //        param                  ключ NVS           поле            мин  макс            по умолч.              флаги                баннер
  SET_STR  ("wifi_ssid",           "wifi_ssid",       wifi_ssid,      0,   WIFI_SSID_MAX,  DEFAULT_WIFI_SSID,     SF_TRIM,             "wifi"),
  SET_STR  ("wifi_pass",           "wifi_pass",       wifi_pass,      8,   WIFI_PASS_MAX,  DEFAULT_WIFI_PASS,     SF_SECRET,           "wifi"),
  SET_STR  ("ap_ssid",             "ap_ssid",         ap_ssid,        1,   WIFI_SSID_MAX,  DEFAULT_AP_SSID,       SF_TRIM,             "ap"),
  SET_STR  ("ap_pass",             "ap_pass",         ap_pass,        8,   WIFI_PASS_MAX,  DEFAULT_AP_PASS,       SF_SECRET,           "ap"),
  // --- Отправка телеметрии (ключи NVS не длиннее 15 символов) ---
  SET_STR  ("telemetry_url",       "telem_url",       telemetry_url,  TELEMETRY_URL_MIN, TELEMETRY_URL_MAX, DEFAULT_TELEMETRY_URL, SF_TRIM,    "telemetry"),
  SET_STR  ("telemetry_token",     "telem_token",     telemetry_token, TELEMETRY_TOKEN_MIN, TELEMETRY_TOKEN_MAX, DEFAULT_TELEMETRY_TOKEN, SF_SECRET | SF_TRIM, "telemetry"),
  SET_STR  ("device_label",        "device_label",    device_label,   1,   DEVICE_LABEL_MAX, DEFAULT_DEVICE_LABEL, SF_TRIM,            "telemetry"),
};
static const size_t SETTING_DEFS_COUNT = sizeof(SETTING_DEFS) / sizeof(SETTING_DEFS[0]);

uint8_t *fieldAddr(Settings &s, const SettingDef &d) {
  return (uint8_t *)&s + d.offset;
}

const uint8_t *fieldAddr(const Settings &s, const SettingDef &d) {
  return (const uint8_t *)&s + d.offset;
}

// Empty router password means an open upstream network; empty token disables cloud.
// The device AP itself always requires WPA2.
bool settingStringValid(const SettingDef &d, const char *v) {
  size_t len = strlen(v);
  if (len == 0) return strcmp(d.param, "ap_pass") != 0 && (d.lo == 0 || (d.flags & SF_SECRET));
  if (len < (size_t)d.lo || len > (size_t)d.hi) return false;
  // Эти три значения уходят на сервер, где проверяются теми же правилами (см. Telemetry.h).
  if (strcmp(d.param, "telemetry_url") == 0) return telemetryUrlValid(v);
  if (strcmp(d.param, "telemetry_token") == 0) return telemetryTokenValid(v);
  if (strcmp(d.param, "device_label") == 0) return deviceLabelValid(v);
  return true;
}

void orderRange(int &lo, int &hi) {
  if (lo > hi) { int t = lo; lo = hi; hi = t; }
}

// Подстраховка от "перевёрнутых" границ (min > max): меняем местами, чтобы устройство
// не осталось с невозможным диапазоном. Срабатывает и на форму, и на испорченный NVS.
void normalizeRanges(Settings &s) {
  orderRange(s.led_min_limit, s.led_max_limit);
  orderRange(s.fan1_min_limit, s.fan1_max_limit);
  orderRange(s.fan2_min_limit, s.fan2_max_limit);
}

// Загрузка всех настроек из NVS. Числа клэмпятся и здесь — на случай испорченных
// значений; строки недопустимой длины откатываются на дефолт (раньше так защищалась
// только точка доступа, чтобы не остаться без доступа к плате).
void loadSettingsFromNVS(Settings &s) {
  memset(&s, 0, sizeof(Settings));
  for (size_t i = 0; i < SETTING_DEFS_COUNT; i++) {
    const SettingDef &d = SETTING_DEFS[i];
    uint8_t *p = fieldAddr(s, d);
    switch (d.type) {
      case ST_FLOAT:
        // NaN/inf in NVS (e.g. saved by an older build) falls back to the default.
        *(float *)p = clampSettingFloat(preferences.getFloat(d.nvsKey, d.def), d.lo, d.hi, d.def);
        break;
      case ST_INT:
        *(int *)p = clampInt(preferences.getInt(d.nvsKey, (int)d.def), (int)d.lo, (int)d.hi);
        break;
      case ST_U8:
        *(uint8_t *)p = (uint8_t)clampInt(preferences.getUChar(d.nvsKey, (uint8_t)d.def), (int)d.lo, (int)d.hi);
        break;
      case ST_BOOL:
        *(bool *)p = preferences.getBool(d.nvsKey, d.def != 0);
        break;
      case ST_U32:
        *(uint32_t *)p = preferences.getUInt(d.nvsKey, (uint32_t)d.def);
        break;
      case ST_STRING: {
        String v = preferences.getString(d.nvsKey, d.defStr);
        if (!settingStringValid(d, v.c_str())) v = d.defStr;
        strlcpy((char *)p, v.c_str(), d.size);
        break;
      }
    }
  }
  normalizeRanges(s);
}

// "ap,wifi" — без повторов, если в одной группе отклонено сразу несколько полей
void addErrorGroup(String &list, const char *group) {
  String padded = "," + list + ",";
  if (padded.indexOf("," + String(group) + ",") >= 0) return;
  if (list.length()) list += ",";
  list += group;
}

// Применяет поля формы к копии настроек (значения клэмпятся в разумные пределы, даже если
// запрос пришёл в обход веб-формы). Возвращает отклонённые группы через запятую — по ним
// страница показывает баннеры ошибок.
String applyFormToSettings(AsyncWebServerRequest *request, Settings &s) {
  String rejected;
  for (size_t i = 0; i < SETTING_DEFS_COUNT; i++) {
    const SettingDef &d = SETTING_DEFS[i];
    if (d.flags & SF_NO_FORM) continue;
    uint8_t *p = fieldAddr(s, d);

    if (d.type == ST_STRING && (d.flags & SF_SECRET)) {
      // Пустое поле пароля = "оставить как есть" (форма его не подставляет). Стереть
      // пароль можно только явно — галочкой <param>_clear ("сеть без пароля").
      String clearParam = String(d.param) + "_clear";
      if (request->hasParam(clearParam, true) && request->getParam(clearParam, true)->value() == "1") {
        if (strcmp(d.param, "ap_pass") == 0) { addErrorGroup(rejected, "ap"); continue; }
        ((char *)p)[0] = '\0';
        continue;
      }
    }

    if (!request->hasParam(d.param, true)) continue;
    String v = request->getParam(d.param, true)->value();

    switch (d.type) {
      // toFloat() accepts "nan" and "inf": such input keeps the current value.
      case ST_FLOAT: *(float *)p = clampSettingFloat(v.toFloat(), d.lo, d.hi, *(float *)p); break;
      case ST_INT:   *(int *)p = clampInt(v.toInt(), (int)d.lo, (int)d.hi); break;
      case ST_U8:    *(uint8_t *)p = (uint8_t)clampInt(v.toInt(), (int)d.lo, (int)d.hi); break;
      case ST_BOOL:  *(bool *)p = (v == "1"); break;
      case ST_U32:   *(uint32_t *)p = strtoul(v.c_str(), nullptr, 10); break;
      case ST_STRING:
        if (d.flags & SF_TRIM) v.trim();
        if ((d.flags & SF_SECRET) && v.length() == 0) break;  // пусто = не менять
        if (settingStringValid(d, v.c_str())) strlcpy((char *)p, v.c_str(), d.size);
        else if (d.errGroup) addErrorGroup(rejected, d.errGroup);
        break;
    }
  }
  normalizeRanges(s);
  return rejected;
}

// Пишет в NVS только изменившиеся поля: неизменные значения не переписываются на каждое
// сохранение любой из форм, и флеш изнашивается меньше
void saveChangedSettings(const Settings &prev, const Settings &next) {
  for (size_t i = 0; i < SETTING_DEFS_COUNT; i++) {
    const SettingDef &d = SETTING_DEFS[i];
    const uint8_t *a = fieldAddr(prev, d);
    const uint8_t *b = fieldAddr(next, d);
    bool changed = (d.type == ST_STRING) ? strcmp((const char *)a, (const char *)b) != 0
                                         : memcmp(a, b, d.size) != 0;
    if (!changed) continue;
    switch (d.type) {
      case ST_FLOAT:  preferences.putFloat(d.nvsKey, *(const float *)b); break;
      case ST_INT:    preferences.putInt(d.nvsKey, *(const int *)b); break;
      case ST_U8:     preferences.putUChar(d.nvsKey, *(const uint8_t *)b); break;
      case ST_BOOL:   preferences.putBool(d.nvsKey, *(const bool *)b); break;
      case ST_U32:    preferences.putUInt(d.nvsKey, *(const uint32_t *)b); break;
      case ST_STRING: preferences.putString(d.nvsKey, (const char *)b); break;
    }
  }
}

// Even authenticated clients receive only <param>_set flags, never stored secrets.
void appendSettingsJson(String &json, const Settings &s) {
  for (size_t i = 0; i < SETTING_DEFS_COUNT; i++) {
    const SettingDef &d = SETTING_DEFS[i];
    const uint8_t *p = fieldAddr(s, d);
    json += "\"";
    json += d.param;
    if (d.type == ST_STRING && (d.flags & SF_SECRET)) {
      json += "_set\":";
      json += (((const char *)p)[0] != '\0') ? "true" : "false";
      json += ",";
      continue;
    }
    json += "\":";
    switch (d.type) {
      case ST_FLOAT:  json += String(*(const float *)p, 1); break;
      case ST_INT:    json += String(*(const int *)p); break;
      case ST_U8:     json += String((int)*(const uint8_t *)p); break;
      case ST_BOOL:   json += (*(const bool *)p) ? "true" : "false"; break;
      case ST_U32:    json += String((unsigned long)*(const uint32_t *)p); break;
      case ST_STRING: json += "\""; json += jsonEscape((const char *)p); json += "\""; break;
    }
    json += ",";
  }
}

// Пауза перед первой попыткой подключиться к роутеру и период повторных попыток.
static const uint32_t WIFI_FIRST_DELAY_MS = 5000;
static const uint32_t WIFI_RETRY_MS       = 120000;

// Фоновая задача на Core 0: держит STA-подключение к домашнему роутеру, чтобы
// веб-интерфейс был доступен не только через точку доступа платы. Наружу ничего
// не отправляет — телеметрия в облако из прошивки убрана.
void vWifiTask(void *pvParameters) {
  // С какими данными роутера поднято текущее STA-соединение
  char connectedSsid[WIFI_SSID_MAX + 1] = "";
  char connectedPass[WIFI_PASS_MAX + 1] = "";

  // Первую попытку делаем через 5 с после старта, дальше повторяем раз в 2 минуты.
  // Ждём уведомление, а не просто таймаут: сохранение настроек Wi-Fi будит задачу
  // сразу, не дожидаясь конца сна.
  TickType_t sleepTicks = pdMS_TO_TICKS(WIFI_FIRST_DELAY_MS);

  for (;;) {
    ulTaskNotifyTake(pdTRUE, sleepTicks);
    sleepTicks = pdMS_TO_TICKS(WIFI_RETRY_MS);

    // Своя копия: глобальные данные других задач здесь напрямую не читаются
    Settings cfg = settingsSnapshot();

    if (cfg.wifi_ssid[0] == '\0') {
      if (WiFi.status() == WL_CONNECTED) WiFi.disconnect();
      continue;
    }

    // Данные роутера поменяли через веб — рвём старое соединение. Иначе плата так и
    // сидела бы на прежней сети до первого обрыва связи.
    bool credsChanged = strcmp(cfg.wifi_ssid, connectedSsid) != 0 || strcmp(cfg.wifi_pass, connectedPass) != 0;
    if (credsChanged && WiFi.status() == WL_CONNECTED) {
      logPrintln("[Core 0] Настройки Wi-Fi изменились — переподключаемся.");
      WiFi.disconnect();
      vTaskDelay(pdMS_TO_TICKS(200));
    }

    if (WiFi.status() == WL_CONNECTED) continue;

    // Режим AP+STA поднят один раз в setup() и больше не переключается. Раньше задача
    // каждые 2 минуты делала mode()/disconnect(true), из-за чего точка доступа
    // передёргивалась и телефон отваливался от веб-интерфейса прямо во время работы.
    WiFi.begin(cfg.wifi_ssid, cfg.wifi_pass);
    strlcpy(connectedSsid, cfg.wifi_ssid, sizeof(connectedSsid));
    strlcpy(connectedPass, cfg.wifi_pass, sizeof(connectedPass));
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
      vTaskDelay(pdMS_TO_TICKS(500));
      attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
      logPrintf("[Core 0] Роутер: подключено, веб-интерфейс на http://%s/\n",
                WiFi.localIP().toString().c_str());
    } else {
      // Останавливаем поиск сети до следующей попытки. Пока STA сканирует эфир, радио уходит
      // с канала точки доступа, и веб-интерфейс у подключённого телефона работает с обрывами.
      WiFi.disconnect();
      logPrintln("[Core 0] Нет связи с роутером — повторим через 2 минуты.");
    }
  }
}

// ---------------------------------------------------------------------------------------
// ОТПРАВКА ТЕЛЕМЕТРИИ НА СЕРВЕР ПАНЕЛИ (dashboard/, формат — dashboard/README.md)
// ---------------------------------------------------------------------------------------
// Текст в JSON с сохранением переводов строк: jsonEscape() для настроек заменяет их пробелами,
// а строки журнала должны дойти до сервера по одной.
static void appendJsonText(String &out, const char *s, size_t len) {
  static const char hex[] = "0123456789abcdef";
  for (size_t i = 0; i < len; i++) {
    const uint8_t c = (uint8_t)s[i];
    switch (c) {
      case '"':  out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += (char)c;
    }
  }
}

static void addNumber(String &p, const char *key, float v, int digits) {
  if (!finiteFloat(v)) return; // NaN или бесконечность в JSON недопустимы: поле просто не уходит
  p += ",\""; p += key; p += "\":"; p += String(v, digits);
}

static void addInteger(String &p, const char *key, long v) {
  p += ",\""; p += key; p += "\":"; p += String(v);
}

static void addFlag(String &p, const char *key, bool v) {
  p += ",\""; p += key; p += v ? "\":true" : "\":false";
}

// Тело запроса: показания и (если есть) новая порция журнала. window — копия кольцевого
// буфера, chunk — выбранная часть (позиции считаются от windowStart).
static String buildTelemetryPayload(const Settings &cfg, const State &st, const char *window,
                                    uint32_t windowStart, const LogChunk &chunk) {
  String p;
  p.reserve(900 + chunk.len + chunk.len / 8);
  p += "{\"device\":\"";
  appendJsonText(p, cfg.device_label, strlen(cfg.device_label));
  p += "\",\"sample\":{\"led\":";
  p += String(st.led_pwm);
  if (st.sht_online) {
    addNumber(p, "temp", st.temp, 2);
    addNumber(p, "hum", st.hum, 2);
  }
  addInteger(p, "fan1", st.fan1_pwm);
  addInteger(p, "fan2", st.fan2_pwm);
  if (st.power_ok) {
    addNumber(p, "power_w", st.power_w, 1);
    addNumber(p, "bus_v", st.bus_v, 2);
    addNumber(p, "current_a", st.current_a, 3);
  }
  addNumber(p, "energy_kwh", st.energy_wh / 1000.0f, 3);
  addFlag(p, "pump", st.pump_active);
  addFlag(p, "heater", st.heater_active);
  addFlag(p, "led_blocked", st.led_supply_blocked);
  addFlag(p, "sht_ok", st.sht_online);
  addFlag(p, "rtc_ok", st.rtc_online);
  addFlag(p, "clock_trusted", st.clock_trusted);
  addFlag(p, "sd_ok", sd_online);
  addFlag(p, "power_ok", st.power_ok);
  addFlag(p, "water_sensor", cfg.water_sensor_enabled);
  addFlag(p, "water_present", st.water_present);
  addInteger(p, "rssi", WiFi.RSSI());
  addInteger(p, "heap_free", (long)ESP.getFreeHeap());
  addInteger(p, "uptime_s", (long)(millis() / 1000));
  p += ",\"fw\":\"" FIRMWARE_ID "\"}";
  if (chunk.len > 0) {
    p += ",\"log\":{\"boot\":\"";
    p += boot_id;
    p += "\",\"from\":";
    p += String((unsigned long)chunk.from);
    p += ",\"text\":\"";
    appendJsonText(p, window + (chunk.from - windowStart), chunk.len);
    p += "\"}";
  }
  p += "}";
  return p;
}

// Код ответа сервера или отрицательный код ошибки HTTPClient. Сертификат сервера проверяется
// по закреплённому корню (DashCA.h), для этого нужно время UTC.
static int postTelemetry(const Settings &cfg, const String &payload) {
  WiFiClientSecure client;
  client.setCACert(DASH_ROOT_CA);
  client.setHandshakeTimeout(15);
  HTTPClient http;
  http.setTimeout(10000);
  http.setReuse(false);
  if (!http.begin(client, String(cfg.telemetry_url))) return -101;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + cfg.telemetry_token);
  const int code = http.POST(payload);
  http.end();
  return code;
}

// Фоновая задача на Core 0: раз в минуту шлёт показания и новую часть журнала. Отправка
// только читает снимки состояния и настроек и в работу платы не вмешивается: сбой сети,
// сервера или сертификата никак не влияет на свет, вентиляторы, нагрев и полив.
void vTelemetryTask(void *pvParameters) {
  static char logWindow[LOG_BUFFER_SIZE]; // копия кольцевого буфера: 4 КБ на стеке не нужны
  uint32_t logSent = 0;                   // сколько байт журнала сервер уже принял
  int lastCode = 0;
  uint32_t lastReportMs = 0;
  TickType_t sleepTicks = pdMS_TO_TICKS(TELEMETRY_FIRST_DELAY_MS);

  for (;;) {
    // Будильник — уведомление при сохранении настроек телеметрии, иначе таймаут.
    ulTaskNotifyTake(pdTRUE, sleepTicks);
    sleepTicks = pdMS_TO_TICKS(TELEMETRY_PERIOD_MS);

    const Settings cfg = settingsSnapshot();
    if (cfg.telemetry_token[0] == '\0' || WiFi.status() != WL_CONNECTED) continue;

    int code;
    uint32_t sentTo = logSent;
    if (time(nullptr) < UTC_2024) {
      // Системные часы (UTC) нужны только для проверки срока сертификата; RTC платы они не трогают.
      configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
      for (int i = 0; time(nullptr) < UTC_2024 && i < 40; ++i) vTaskDelay(pdMS_TO_TICKS(250));
    }
    if (time(nullptr) < UTC_2024) {
      code = -100;
    } else {
      const State st = stateSnapshot();
      uint32_t windowStart = 0;
      const size_t n = logCopyWindow(logWindow, windowStart);
      const LogChunk chunk = chooseLogChunk(logWindow, n, windowStart, logSent, TELEMETRY_MAX_LOG_CHUNK);
      code = postTelemetry(cfg, buildTelemetryPayload(cfg, st, logWindow, windowStart, chunk));
      if (code == 200 && chunk.len > 0) sentTo = chunk.from + (uint32_t)chunk.len;
    }
    if (code == 200) logSent = sentTo;

    // Итог пишем в журнал платы при смене результата и раз в полчаса при затянувшейся ошибке;
    // строки доходят до сервера, когда связь вернётся.
    const uint32_t now = millis();
    if (telemetryReportDue(lastCode, code, now, lastReportMs, TELEMETRY_REPORT_REPEAT_MS)) {
      lastReportMs = now;
      if (code == 200) logPrintln(lastCode == 0 ? "[Telemetry] Отправка на сервер работает."
                                                : "[Telemetry] Отправка восстановилась.");
      else if (code == -100) logPrintln("[Telemetry] Ошибка отправки: нет времени UTC, сертификат сервера не проверить.");
      else if (code > 0) logPrintf("[Telemetry] Ошибка отправки: сервер ответил HTTP %d%s\n", code,
                                   code == 401 ? " (проверьте токен в настройках)" : "");
      else logPrintf("[Telemetry] Ошибка отправки: нет соединения с сервером (код %d)\n", code);
    }
    lastCode = code;
  }
}

// Принудительное восстановление I2C-шины: если одно из устройств (SHT4x/RTC) зависло
// посреди транзакции и держит SDA в LOW, обычный Wire.begin()/sensor.begin() это НЕ чинит —
// нужно вручную "протактовать" SCL, чтобы slave-устройство доотправило начатый байт и
// освободило линию, затем сформировать STOP и переинициализировать Wire.
void recoverI2CBus() {
  Wire.end(); // Stop the driver before temporarily taking over its GPIOs.
  pinMode(I2C_SDA_PIN, INPUT_PULLUP);
  pinMode(I2C_SCL_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(I2C_SCL_PIN, HIGH);

  if (digitalRead(I2C_SDA_PIN) == HIGH) {
    // Шина уже свободна — восстанавливать нечего, просто переинициализируем Wire
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    return;
  }

  logPrintln("[I2C] SDA удерживается в LOW — пробуем восстановить шину...");

  // До 9 тактов SCL — по спецификации I2C этого достаточно, чтобы slave, зависший
  // в середине передачи байта, дотактовал его до конца и отпустил SDA
  for (int i = 0; i < 9 && digitalRead(I2C_SDA_PIN) == LOW; i++) {
    digitalWrite(I2C_SCL_PIN, LOW);
    delayMicroseconds(5);
    digitalWrite(I2C_SCL_PIN, HIGH);
    delayMicroseconds(5);
  }

  // Формируем STOP-условие вручную (SDA LOW->HIGH, пока SCL HIGH), чтобы сбросить
  // внутренний автомат состояний slave-устройства в исходное состояние
  pinMode(I2C_SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(I2C_SDA_PIN, LOW);
  delayMicroseconds(5);
  digitalWrite(I2C_SCL_PIN, HIGH);
  delayMicroseconds(5);
  digitalWrite(I2C_SDA_PIN, HIGH);
  delayMicroseconds(5);

  pinMode(I2C_SDA_PIN, INPUT_PULLUP);
  if (digitalRead(I2C_SDA_PIN) == LOW) {
    logPrintln("[I2C] Восстановить шину не удалось — SDA всё ещё LOW.");
  } else {
    logPrintln("[I2C] Шина восстановлена.");
  }

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
}

// Читает датчик уровня воды. MicroGarden_V1: внешняя подтяжка 10k к 3,3 В, GPIO14 = INPUT;
// датчик замыкает его на GND, когда вода есть — LOW = "вода в норме", HIGH = "воды нет".
// Если у конкретного датчика логика обратная — поменяй местами LOW/HIGH здесь, в одном месте.
bool isWaterAvailable() {
  return digitalRead(WATER_LEVEL_PIN) == LOW;
}

// --- Диагностика I2C и SHT4x. Всё пишется в веб-консоль, чтобы причину отказа было видно без USB ---

// Какие устройства отвечают на шине. Печатается при каждом старте и при отказе датчика.
void logI2CScan() {
  String found;
  int count = 0;
  bool sawSht = false, sawShtB = false;
  for (uint8_t addr = 0x08; addr < 0x78; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() != 0) continue;
    const char *name = addr == 0x44 ? " (SHT4x)" : addr == 0x45 ? " (SHT4x-B?)" :
                       addr == MCP7940N_ADDR ? " (MCP7940N)" : addr == INA238_ADDR ? " (INA238)" : "";
    char item[40];
    snprintf(item, sizeof(item), " 0x%02X%s", addr, name);
    found += item;
    count++;
    if (addr == 0x44) sawSht = true;
    if (addr == 0x45) sawShtB = true;
  }
  String line = "[I2C] Отвечают устройств: " + String(count);
  if (count) { line += ":"; line += found; }
  logPrintln(line);
  if (sawShtB && !sawSht) {
    logPrintln("[I2C] На 0x45 отвечает устройство — похоже на SHT4x варианта B (-BD1B). Прошивка ищет датчик на 0x44.");
  }
}

// Adafruit getEvent() на любую ошибку отвечает одним false. Здесь то же измерение
// делается вручную, и в лог пишется, на каком шаге оно ломается: нет ответа,
// короткое чтение, ошибка CRC или значения вне рабочего диапазона.
void logSht4xDiagnosis() {
  const uint8_t addr = 0x44;
  Wire.beginTransmission(addr);
  Wire.write(uint8_t(0xFD)); // Измерение с высокой точностью, как у Adafruit по умолчанию
  uint8_t err = Wire.endTransmission();
  if (err != 0) {
    // 2 — адрес не подтверждён (датчика не слышно на шине), 5 — таймаут, 4 — прочее
    logPrintf("[SHT4x] Диагностика: нет ответа на 0x44 (код I2C %u)\n", (unsigned)err);
    return;
  }
  delay(10);
  uint8_t raw[6] = {0};
  size_t n = Wire.requestFrom(addr, size_t(6), true);
  for (size_t i = 0; i < n && i < sizeof(raw); i++) raw[i] = Wire.read();
  if (n != 6) {
    logPrintf("[SHT4x] Диагностика: команда принята, но прочитано %u байт из 6\n", (unsigned)n);
    return;
  }
  bool crcT = sensirionCrc8(raw, 2) == raw[2];
  bool crcH = sensirionCrc8(raw + 3, 2) == raw[5];
  float t = -45.0f + 175.0f * (float)((raw[0] << 8) | raw[1]) / 65535.0f;
  float h = -6.0f + 125.0f * (float)((raw[3] << 8) | raw[4]) / 65535.0f;
  logPrintf("[SHT4x] Диагностика: %02X %02X %02X %02X %02X %02X, CRC %s/%s, T=%.2f RH=%.2f\n",
            raw[0], raw[1], raw[2], raw[3], raw[4], raw[5],
            crcT ? "ok" : "ОШИБКА", crcH ? "ok" : "ОШИБКА", t, h);
}

const char *rtcName() {
  return "MCP7940N";
}

bool readRtcRegisters(uint8_t reg, uint8_t *out, size_t count) {
  Wire.beginTransmission(MCP7940N_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(MCP7940N_ADDR, count, true) != count) return false;
  for (size_t i = 0; i < count; ++i) out[i] = Wire.read();
  return true;
}

bool writeRtcRegisters(uint8_t reg, const uint8_t *data, size_t count) {
  Wire.beginTransmission(MCP7940N_ADDR);
  Wire.write(reg);
  const bool complete = Wire.write(data, count) == count;
  return Wire.endTransmission() == 0 && complete;
}

bool rtcBegin() {
  uint8_t raw[7];
  rtc_present = readRtcRegisters(0x00, raw, sizeof(raw));
  if (!rtc_present) {
    state.rtc_status = RTC_NO_RESPONSE;
    return false;
  }
  if (!(raw[3] & MCP7940N_VBATEN)) {
    const uint8_t weekday = (raw[3] & 0x07) | MCP7940N_VBATEN;
    if (!writeRtcRegisters(0x03, &weekday, 1)) return false;
    // Writing RTCWKDAY also clears PWRFAIL (DS20005010J); it is not a validity flag.
  }
  // Never start a stopped, possibly stale calendar here. ST is set only after the
  // user supplies a complete valid date/time; readRTC() requires ST and OSCRUN.
  return true;
}

// 3 s: the 32 kHz crystal can take over a second to start, longer when cold.
bool waitRtcOscillator(bool running) {
  const uint32_t started = millis();
  do {
    stopPumpAtDeadline(); // Setting the clock must not extend an active watering.
    uint8_t weekday;
    if (!readRtcRegisters(0x03, &weekday, 1)) return false;
    if (bool(weekday & MCP7940N_OSCRUN) == running) return true;
    delay(5);
  } while (elapsedMs(millis(), started) < 3000);
  return false; // A missing/stuck crystal must never make a write look successful.
}

// Microchip DS20005010J section 5.3: stop, wait for OSCRUN=0, load the calendar, then start.
// Month and year are written first, on their own, so the date is never loaded against a
// stale month (e.g. the 31st while the chip still holds a 30-day month). ST is written last,
// so a failed or partial write leaves the clock stopped and therefore untrusted.
// Every failed step is logged: the console then says what to check on the board.
bool rtcAdjust(const DateTime &time) {
  uint8_t raw[7];
  const RtcFields fields{time.year(), time.month(), time.day(), time.hour(), time.minute(), time.second()};
  if (!encodeMcp7940n(fields, time.dayOfTheWeek() + 1, raw)) {
    logPrintln("[RTC] Установка: дата вне диапазона 2024–2099.");
    return false;
  }
  const uint8_t control = 0; // Crystal oscillator; unused MFP square wave/alarms off.
  const uint8_t stopped = 0;
  if (!writeRtcRegisters(0x07, &control, 1) || !writeRtcRegisters(0x00, &stopped, 1)) {
    logPrintln("[RTC] Установка: MCP7940N не принял команду остановки (I2C).");
    return false;
  }
  if (!waitRtcOscillator(false)) {
    logPrintln("[RTC] Установка: генератор не остановился за 3 с (OSCRUN=1).");
    return false;
  }
  const uint8_t secondsWithStart = raw[0];
  raw[0] &= ~MCP7940N_ST;
  if (!writeRtcRegisters(0x05, raw + 5, 2) || !writeRtcRegisters(0x00, raw, 5)) {
    logPrintln("[RTC] Установка: ошибка I2C при записи даты и времени.");
    return false;
  }
  if (!writeRtcRegisters(0x00, &secondsWithStart, 1)) {
    logPrintln("[RTC] Установка: ошибка I2C при запуске часов.");
    return false;
  }
  if (!waitRtcOscillator(true)) {
    logPrintln("[RTC] Установка: кварц Y301 не запустился за 3 с (OSCRUN=0). Проверьте кварц и его пайку.");
    return false;
  }
  return true;
}

// One read: RTC_OK with a valid running calendar, RTC_NOT_SET when the chip answers but the
// clock is stopped or holds an invalid date, RTC_NO_RESPONSE on an I2C error.
RtcStatus readRtcOnce(DateTime &result) {
  // One sequential read uses the MCP7940N's rollover-safe snapshot buffers (section 5.3).
  uint8_t raw[7];
  RtcFields fields{};
  if (!readRtcRegisters(0x00, raw, sizeof(raw))) return RTC_NO_RESPONSE;
  if (!decodeMcp7940n(raw, fields)) return RTC_NOT_SET;
  result = DateTime(fields.year, fields.month, fields.day, fields.hour, fields.minute, fields.second);
  return RTC_OK;
}

// Up to three tries: a single I2C glitch must not throw the clock out of trust for a minute.
bool readRTC(DateTime &result) {
  RtcStatus status = RTC_NO_RESPONSE;
  for (int attempt = 0; attempt < 3; ++attempt) {
    if (attempt) delay(2);
    status = readRtcOnce(result);
    if (status == RTC_OK) break;
  }
  state.rtc_status = status;
  return status == RTC_OK;
}

// Безопасное время: из RTC или из программного бэкапа. Вызывается только из setup()/loop().
DateTime getSafeDateTime() {
  if (state.rtc_online) {
    DateTime now;
    if (readRTC(now)) {
      state.clock_trusted = true;
      backup_unixtime = now.unixtime(); // Синхронизируем бэкап
      last_rtc_check_ms = millis();     // ...и точку отсчёта программного таймера
      backup_clock_synced = true;
      return now;
    }
    logPrintln("[КРИТИКА] RTC вернул некорректную дату! Переход на программный таймер.");
    state.rtc_online = false;
  }

  // Программный таймер идёт от последнего чтения RTC или от времени, введённого владельцем (точность
  // порядка секунд в сутки). Пока он выставлен таким источником (backup_clock_synced), на нём работают
  // и расписание полива, и свет без измерителя питания; clock_trusted при этом остаётся false, чтобы
  // RGB и страница показывали отказ RTC.
  state.clock_trusted = false;
  advanceBackupClock(millis(), last_rtc_check_ms, backup_unixtime);
  return DateTime(backup_unixtime);
}

// Время для веб-обработчиков: готовый снимок, без обращения к железу с чужого ядра
DateTime getWebDateTime() {
  return DateTime((uint32_t)cached_unixtime);
}

// Периодически пробует восстановить RTC после сбоя, без перезагрузки платы —
// та же схема, что и для датчика SHT4x. Нужно несколько подряд успешных
// попыток чтения вменяемой даты, прежде чем снова начать доверять RTC.
void tryRecoverRTC(unsigned long currentMillis) {
  if (state.rtc_online) return;
  if (currentMillis - last_rtc_retry < RTC_RETRY_INTERVAL_MS) return;
  last_rtc_retry = currentMillis;
  recoverI2CBus(); // Освобождаем шину на случай, если она физически "залипла"

  bool retry_ok = false;
  if (rtcBegin()) {
    DateTime now;
    // 2024..2099 — грубая защита от "проснувшегося после разряда батарейки" RTC,
    // который обычно сбрасывается на заводскую дату (например, 2000 или 2021 год)
    if (readRTC(now)) {
      retry_ok = true;
      rtc_recovery_streak++;
      logPrintf("[RTC] Попытка восстановления %d/%d успешна\n", rtc_recovery_streak, RTC_RECOVERY_STREAK_NEEDED);
      if (rtc_recovery_streak >= RTC_RECOVERY_STREAK_NEEDED) {
        state.rtc_online = true;
        state.clock_trusted = true;
        backup_unixtime = now.unixtime();
        last_rtc_check_ms = millis();
        backup_clock_synced = true;
        rtc_recovery_streak = 0;
        logPrintf("[RTC] %s восстановлен, выходим из программного таймера.\n", rtcName());
      }
    }
  }
  if (!retry_ok) {
    rtc_recovery_streak = 0; // Сбрасываем счётчик серии при любой неудачной попытке
  }
}

// --- INA238: total 24 V power through the two parallel 7 mOhm shunts ---
// VSHUNT and VBUS raw registers; bus voltage is connected directly, no R113 correction.
// Only loop() talks to it. A silent chip shows "нет данных" in the UI and changes nothing else.
bool ina238Read(uint8_t reg, uint16_t &value) {
  Wire.beginTransmission(INA238_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(INA238_ADDR, size_t(2), true) != 2) return false;
  const uint8_t hi = Wire.read();
  value = uint16_t(hi) << 8 | uint8_t(Wire.read());
  return true;
}

bool ina238Write(uint8_t reg, uint16_t value) {
  Wire.beginTransmission(INA238_ADDR);
  Wire.write(reg);
  Wire.write(uint8_t(value >> 8));
  Wire.write(uint8_t(value & 0xff));
  return Wire.endTransmission() == 0;
}

struct Ina238Bus {
  bool read(uint8_t reg, uint16_t &value) { return ina238Read(reg, value); }
  bool write(uint8_t reg, uint16_t value) { return ina238Write(reg, value); }
};
static Ina238Bus power_bus;
static Ina238<Ina238Bus> power_meter(power_bus);

static bool power_online = false;
static uint32_t power_last_poll_ms = 0, power_last_retry_ms = 0, power_last_sample_ms = 0;
static uint8_t power_failures = 0;
static uint32_t energy_day = 0;   // Day code energy_wh belongs to
static DailyRuntime heater_runtime = {0, 0}; // Наработка нагревателя за сутки платы
static uint32_t heater_runtime_ms = 0;       // millis() предыдущего шага подсчёта
static double energy_wh = 0;
static double csv_power_ws = 0;   // For the 10-minute CSV average
static uint32_t csv_power_ms = 0;

// Average watts since the previous CSV row; NaN when the meter gave nothing in that time.
float takeCsvAveragePower() {
  const float avg = csv_power_ms >= 1000 ? float(csv_power_ws * 1000.0 / csv_power_ms) : NAN;
  csv_power_ws = 0;
  csv_power_ms = 0;
  return avg;
}

// Сколько нагреватель проработал за сутки платы. Считается на каждом проходе loop() по настоящему
// времени между проходами, поэтому долгие операции (например, установка часов) в наработку входят:
// пока они идут, пин нагревателя остаётся включённым. Как и энергия, счёт не хранится в NVS:
// после перезагрузки он идёт заново с запуска платы.
void updateHeaterRuntime(uint32_t nowMs, uint32_t todayCode) {
  const uint32_t dt = elapsedMs(nowMs, heater_runtime_ms);
  heater_runtime_ms = nowMs;
  heater_runtime = dailyRuntimeStep(heater_runtime, todayCode, state.heater_active, dt);
  state.heater_on_s = heater_runtime.ms / 1000;
}

void updatePowerMeter(uint32_t nowMs, uint32_t todayCode, float calPct) {
  if (todayCode != energy_day) {   // Midnight (or the clock was set to another day)
    energy_day = todayCode;
    energy_wh = 0;
  }
  state.energy_wh = float(energy_wh);
  if (!power_online) {
    if (power_last_retry_ms != 0 && elapsedMs(nowMs, power_last_retry_ms) < 30000) return;
    power_last_retry_ms = nowMs ? nowMs : 1;
    if (!power_meter.begin()) return;
    power_online = true;
    power_failures = 0;
    power_last_poll_ms = nowMs;
    power_last_sample_ms = 0;
    logPrintln("[INA238] Найден на 0x40, измерение мощности включено.");
    return;
  }
  if (elapsedMs(nowMs, power_last_poll_ms) < 1000) return;
  power_last_poll_ms = nowMs;

  uint16_t shuntRaw = 0, busRaw = 0;
  const bool ok = power_meter.sample(shuntRaw, busRaw);
  if (!ok) {
    state.power_ok = false; // Block LED immediately; never keep a stale successful sample active.
    power_last_sample_ms = 0;
    if (++power_failures >= 3) {
      power_online = false;
      state.power_ok = false;
      power_last_retry_ms = nowMs ? nowMs : 1;
      logPrintln("[INA238] Не отвечает — мощность не показывается, повтор раз в 30 с.");
    }
    return;
  }
  power_failures = 0;
  state.current_a = ina238Amps(int16_t(shuntRaw), 1.0f + calPct / 100.0f);
  state.bus_v = ina238Volts(busRaw);
  state.power_w = state.bus_v * state.current_a;
  state.power_ok = true;
  if (power_last_sample_ms != 0) {
    // Integrate only over a normal 1 s step: a gap (failed reads, a long set-time) is skipped.
    const uint32_t dt = elapsedMs(nowMs, power_last_sample_ms);
    if (dt <= 5000 && state.power_w > 0) {
      energy_wh += double(state.power_w) * dt / 3600000.0;
      csv_power_ws += double(state.power_w) * dt / 1000.0;
    }
    if (dt <= 5000) csv_power_ms += dt;
  }
  power_last_sample_ms = nowMs;
  state.energy_wh = float(energy_wh);
}

String randomSecret() {
  char value[33];
  uint8_t bytes[16];
  esp_fill_random(bytes, sizeof(bytes));
  for (int i = 0; i < 16; ++i) snprintf(value + i * 2, 3, "%02x", bytes[i]);
  return String(value);
}

// Eight easy-to-type characters; keep the longer randomSecret() for CSRF.
String randomPassword() {
  static const char alphabet[] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
  char password[9];
  for (size_t i = 0; i < 8; ++i) {
    uint8_t sample;
    esp_fill_random(&sample, 1);
    password[i] = alphabet[sample & 31];
  }
  password[8] = '\0';
  return String(password);
}

bool isLegacyGeneratedPassword(const String &password) {
  if (password.length() != 32) return false;
  for (size_t i = 0; i < password.length(); ++i) {
    char c = password[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  return true;
}

bool authorize(AsyncWebServerRequest *request) {
  if (!request->authenticate("admin", admin_password.c_str(), "GI")) {
    request->requestAuthentication("GI", true);
    return false;
  }
  if (request->method() == HTTP_POST) {
    String token;
    if (request->hasHeader("X-CSRF-Token")) token = request->getHeader("X-CSRF-Token")->value();
    else if (request->hasParam("csrf_token", true)) token = request->getParam("csrf_token", true)->value();
    if (token != csrf_token) {
      request->send(403, "text/plain", "Reload settings and retry");
      return false;
    }
  }
  return true;
}

// Фатальная ошибка инициализации: выходы к этому моменту выключены, продолжать
// старт нельзя. Останавливаемся навсегда, но WDT сбрасываем — иначе плата
// уходит в перезагрузку каждые 15 с и бесконечно повторяет неудачный старт.
static void haltOnFatalError(const char *reason) {
  if (reason != NULL) Serial.println(reason);
  for (;;) {
    esp_task_wdt_reset();
    delay(1000);
  }
}

void setup() {
  // FIRST: MicroGarden_V1 power outputs off, before Serial, buses, NVS, WiFi or PWM.
  // Preload LOW before enabling outputs, even if a software reset left a HIGH latch.
  // Arduino-ESP32 3.x digitalWrite() cannot preload a pin before pinMode().
  gpio_set_level(gpio_num_t(LED_PWM_PIN), LOW);
  gpio_set_level(gpio_num_t(HEATER_PIN), LOW);
  gpio_set_level(gpio_num_t(PUMP_PIN), LOW);
  pinMode(LED_PWM_PIN, OUTPUT);
  digitalWrite(LED_PWM_PIN, LOW);
  pinMode(HEATER_PIN, OUTPUT);
  digitalWrite(HEATER_PIN, LOW);
  pinMode(PUMP_PIN, OUTPUT);
  digitalWrite(PUMP_PIN, LOW);
  // LOW opens each fan's external PWM wire: full airflow throughout setup/reset.
  for (int pin : {FAN1_PWM_PIN, FAN2_PWM_PIN}) {
    gpio_set_level(gpio_num_t(pin), LOW);
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }
  for (int pin : {RGB_RED_PIN, RGB_GREEN_PIN, RGB_BLUE_PIN}) {
    gpio_set_level(gpio_num_t(pin), LOW);
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }
  state.led_supply_blocked = true;
  Serial.begin(115200);
  // Внутри setup() после инициализации Serial
  #ifdef ESP_IDF_VERSION_VAL
    // Инициализация WDT на 15 секунды
    esp_task_wdt_config_t wdt_config = {
        .timeout_ms = 15000,
        .idle_core_mask = (1 << 0) | (1 << 1), // Мониторим оба ядра
        .trigger_panic = true
    };
    if (esp_task_wdt_reconfigure(&wdt_config) == ESP_ERR_INVALID_STATE) esp_task_wdt_init(&wdt_config);
    esp_task_wdt_add(NULL); // Добавляем текущую задачу (loop)
  #else
    // Старый метод для более старых версий (2.x)
    esp_task_wdt_init(4, true);
    esp_task_wdt_add(NULL);
  #endif
  // Explicit channels keep LED timer 0 separate from the 25 kHz fan timer 1.
  if (!ledcAttachChannel(LED_PWM_PIN, LED_PWM_FREQ, PWM_RES, LED_PWM_CHANNEL) ||
      !ledcAttachChannel(FAN1_PWM_PIN, FAN_PWM_FREQ, PWM_RES, FAN1_PWM_CHANNEL) ||
      !ledcAttachChannel(FAN2_PWM_PIN, FAN_PWM_FREQ, PWM_RES, FAN2_PWM_CHANNEL) ||
      !ledcAttachChannel(RGB_RED_PIN, RGB_PWM_FREQ, PWM_RES, RGB_RED_CHANNEL) ||
      !ledcAttachChannel(RGB_GREEN_PIN, RGB_PWM_FREQ, PWM_RES, RGB_GREEN_CHANNEL) ||
      !ledcAttachChannel(RGB_BLUE_PIN, RGB_PWM_FREQ, PWM_RES, RGB_BLUE_CHANNEL)) {
    for (int pin : {LED_PWM_PIN, FAN1_PWM_PIN, FAN2_PWM_PIN, RGB_RED_PIN, RGB_GREEN_PIN, RGB_BLUE_PIN}) {
      ledcDetach(pin);
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);
    }
    digitalWrite(RGB_RED_PIN, HIGH);
    haltOnFatalError("PWM initialization failed: LED/PTC/pump off, fans at full speed.");
  }
  rgb_ready = true;
  setStatusColor(StatusColor::Blue);
  ledcWrite(LED_PWM_PIN, 0);
  ledcWrite(FAN1_PWM_PIN, fanGpioDuty(PWM_MAX));
  ledcWrite(FAN2_PWM_PIN, fanGpioDuty(PWM_MAX));

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  delay(300); // Даём I2C-шине и датчикам (SHT4x/RTC) стабилизироваться после подачи питания —
              // без этой паузы begin() может провалиться из-за гонки при старте платы
  recoverI2CBus(); // На случай, если шина осталась "залипшей" ещё с прошлого включения

  WiFi.mode(WIFI_STA); // RF entropy before generating passwords, no open AP.
  if (!preferences.begin("grow-box", false)) {
    haltOnFatalError("Cannot initialize settings; outputs remain off.");
  }
  // A one-time upgrade rotates the old publicly documented AP password.
  if (!preferences.getBool("security_v1", false)) {
    String generated = randomPassword();
    if (!preferences.putString("ap_pass", generated) || !preferences.putBool("security_v1", true)) {
      haltOnFatalError("Cannot persist AP password");
    }
  }
  admin_password = preferences.getString("admin_pass", "");
  if (admin_password.length() != 8) {
    admin_password = randomPassword();
    if (!preferences.putString("admin_pass", admin_password)) {
      haltOnFatalError("Cannot persist admin password");
    }
  }
  csrf_token = randomSecret();

  // One-time migration of the previous generator's 32-character hex AP password.
  if (!preferences.getBool("short_pass_v1", false)) {
    String previous = preferences.getString("ap_pass", "");
    if (isLegacyGeneratedPassword(previous) && !preferences.putString("ap_pass", randomPassword())) {
      haltOnFatalError("Cannot persist AP password");
    }
    if (!preferences.putBool("short_pass_v1", true)) {
      haltOnFatalError("Cannot persist password migration");
    }
  }

  Settings cfg;
  loadSettingsFromNVS(cfg);
  if (strlen(cfg.ap_pass) < 8) {
    String generated = randomPassword();
    strlcpy(cfg.ap_pass, generated.c_str(), sizeof(cfg.ap_pass));
    if (!preferences.putString("ap_pass", cfg.ap_pass)) {
      haltOnFatalError("Cannot persist AP password");
    }
  }
  // USB only: credentials never enter the HTTP console buffer.
  Serial.printf("AP password: %s\nWeb login: admin / %s\n", cfg.ap_pass, admin_password.c_str());
  publishSettings(cfg);
  last_watering_day = preferences.getUInt("last_water", 0);
  state.last_water_day = last_watering_day;

  state.is_day = true;
  state.fan1_pwm = state.fan2_pwm = PWM_MAX; // Logical airflow; boot GPIO duty is zero.

  // Безопасная инициализация датчиков с проверкой работоспособности.
  // Пробуем несколько раз с паузой — при старте платы I2C-шина/датчик могут быть
  // ещё не готовы (просадка питания от одновременного старта WiFi/вентиляторов/SD).
  state.sht_online = false;
  for (int i = 0; i < 5 && !state.sht_online; i++) {
    if (sht40.begin()) {
      state.sht_online = true;
    } else {
      logPrintf("[SHT4x] Попытка инициализации %d/5 не удалась, повтор через 200мс...\n", i + 1);
      delay(200);
    }
  }
  if (!state.sht_online) {
    logPrintln("ОШИБКА: SHT4x не найден после 5 попыток!");
    logSht4xDiagnosis();
  }

  state.rtc_online = false;
  for (int i = 0; i < 5 && !state.rtc_online; i++) {
    DateTime initialTime;
    if (rtcBegin() && readRTC(initialTime)) {
      backup_unixtime = initialTime.unixtime();
      last_rtc_check_ms = millis();
      backup_clock_synced = true;
      state.clock_trusted = true;
      state.rtc_online = true;
    } else {
      logPrintf("[RTC] Попытка инициализации %d/5 не удалась, повтор через 200мс...\n", i + 1);
      delay(200);
    }
  }
  if (!state.rtc_online) {
    if (rtc_present) {
      logPrintf("ОШИБКА: %s отвечает, но время недостоверно (потеря питания/батарейки) — выставьте часы.\n", rtcName());
    } else {
      logPrintln("ОШИБКА: MCP7940N (0x6F) не найден после 5 попыток!");
    }
    last_rtc_check_ms = millis();
  }
  logI2CScan(); // Одна строка при каждом старте: кто вообще отвечает на шине

  SPI.begin(SD_SCK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);
  sd_online = SD.begin(SD_CS_PIN, SPI);
  if (!sd_online) logPrintln("SD-карта не обнаружена.");
  if (!SPIFFS.begin(true)) logPrintln("Ошибка SPIFFS!");

  // Датчик уровня воды: внешняя подтяжка 10k; GPIO32 — только выход нагревателя.
  // Если у твоего датчика логика обратная (замыкание = "воды нет") — поменяй LOW на HIGH в isWaterAvailable().
  pinMode(WATER_LEVEL_PIN, INPUT);
  delay(10); // Даём входу установиться: с фильтрующим конденсатором первое чтение иначе ложный LOW
  const bool waterNow = isWaterAvailable();
  water_level = {waterNow, waterNow, (uint32_t)millis()};
  state.water_present = waterNow;

  // GPIO34/35 are unused on Lite; fan header pin 3 is not connected.

  // AP+STA сразу и навсегда: точка доступа больше не передёргивается задачей телеметрии
  WiFi.mode(WIFI_AP_STA);
  // К роутеру подключается только задача телеметрии, раз в две минуты. Фоновое
  // автоподключение при недоступном роутере сканировало эфир непрерывно и мешало точке доступа.
  WiFi.setAutoReconnect(false);
  WiFi.softAP(cfg.ap_ssid, cfg.ap_pass[0] ? cfg.ap_pass : NULL);

  cached_unixtime = getSafeDateTime().unixtime(); // Веб-обработчики читают только этот снимок
  state.clock_usable = state.clock_trusted || backup_clock_synced;
  publishState(); // Первое состояние — до старта задач, которые его читают

  // 4 КБ хватает: с уходом телеметрии из задачи пропал mbedTLS, которому было мало 8.
  xTaskCreatePinnedToCore(vWifiTask, "WifiTask", 4096, NULL, 1, &WifiTaskHandle, 0);
  // Номер запуска нужен серверу панели, чтобы отличить перезагрузку платы от повторной отправки.
  snprintf(boot_id, sizeof(boot_id), "%08x", (unsigned)esp_random());
  xTaskCreatePinnedToCore(vTelemetryTask, "TelemetryTask", 12288, NULL, 1, &TelemetryTaskHandle, 0); // 12 КБ: mbedTLS не влезает в 8

  // The same guard covers pages, static assets, APIs and future routes.
  server.addMiddleware([](AsyncWebServerRequest *request, ArMiddlewareNext next) {
    if (authorize(request)) next();
  });
  DefaultHeaders::Instance().addHeader("Cache-Control", "no-store");
  DefaultHeaders::Instance().addHeader("X-Frame-Options", "DENY");
  DefaultHeaders::Instance().addHeader("X-Content-Type-Options", "nosniff");

  // --- МАРШРУТИЗАЦИЯ ВЕБ-СЕРВЕРА ---
  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request){ request->send(SPIFFS, "/index.html", "text/html"); });
  server.on("/settings", HTTP_GET, [](AsyncWebServerRequest *request){ request->send(SPIFFS, "/settings.html", "text/html"); });
  // Остальная статика (css/js и всё, что появится в data/) раздаётся одним обработчиком —
  // регистрируется ниже, после API-маршрутов, чтобы точно не перехватывать /api/*

  server.on("/api/status", HTTP_GET, [](AsyncWebServerRequest *request){
    State st = stateSnapshot();
    Settings cfg = settingsSnapshot();
    uint32_t start_ts = cfg.start_timestamp;
    DateTime now = getWebDateTime();
    int grow_day = (start_ts > 0 && now.unixtime() >= start_ts) ? ((now.unixtime() - start_ts) / 86400) + 1 : 0;

    String json = "{";
    json += "\"operation_mode\":" + String(cfg.operation_mode) + ","; // Главная показывает режим в шапке
    json += "\"temp\":" + String(st.temp, 2) + ",";
    json += "\"hum\":" + String(st.hum, 2) + ",";
    json += "\"led\":" + String(st.led_pwm) + ",";
    json += "\"fan1\":" + String(st.fan1_pwm) + ",";
    json += "\"fan2\":" + String(st.fan2_pwm) + ",";
    json += "\"fan_tach_available\":false,";
    json += "\"fan1_rpm\":null,\"fan2_rpm\":null,"; // Stable API: Lite has no tach inputs.
    json += "\"led_supply_blocked\":" + String(st.led_supply_blocked ? "true" : "false") + ",";
    json += "\"time\":\"" + now.timestamp(DateTime::TIMESTAMP_TIME) + "\",";
    json += "\"date\":\"" + now.timestamp(DateTime::TIMESTAMP_DATE) + "\",";
    json += "\"is_day\":" + String(st.is_day ? "true" : "false") + ",";
    json += "\"grow_day\":" + String(grow_day) + ",";
    json += "\"sht_online\":" + String(st.sht_online ? "true" : "false") + ","; // Передаем статус в UI
    json += "\"rtc_online\":" + String(st.rtc_online ? "true" : "false") + ",";
    json += "\"clock_trusted\":" + String(st.clock_trusted ? "true" : "false") + ",";
    json += "\"clock_usable\":" + String(st.clock_usable ? "true" : "false") + ","; // RTC или выставленный программный таймер
    json += "\"sd_ok\":" + String(sd_online ? "true" : "false") + ","; // Оповещение главной страницы: SD-карта не найдена
    static const char *const RTC_STATUS_NAMES[] = {"ok", "not_set", "no_response"};
    json += "\"rtc_status\":\"" + String(RTC_STATUS_NAMES[st.rtc_status <= RTC_NO_RESPONSE ? st.rtc_status : uint8_t(RTC_NO_RESPONSE)]) + "\",";
    static const char *const TIME_SET_NAMES[] = {"none", "pending", "ok", "failed"};
    const uint8_t timeSet = time_set_result;
    json += "\"time_set\":\"" + String(TIME_SET_NAMES[timeSet <= TIME_SET_FAILED ? timeSet : uint8_t(TIME_SET_NONE)]) + "\",";
    json += "\"pump_active\":" + String(st.pump_active ? "true" : "false") + ",";
    json += "\"water_sensor_enabled\":" + String(cfg.water_sensor_enabled ? "true" : "false") + ",";
    json += "\"water_present\":" + String(st.water_present ? "true" : "false") + ",";
    // Day code of the last scheduled watering (local date = code * 86400), null = never.
    json += "\"last_water_day\":" + (st.last_water_day ? String(st.last_water_day) : String("null")) + ",";
    // True when the marked day passed without the pump running (tank empty, NVS error, mark moved).
    json += "\"last_water_skipped\":" + String(st.last_water_day && st.last_skip_day == st.last_water_day ? "true" : "false") + ",";
    json += "\"power_ok\":" + String(st.power_ok ? "true" : "false") + ",";
    if (st.power_ok) {
      json += "\"bus_v\":" + String(st.bus_v, 2) + ",";
      json += "\"current_a\":" + String(st.current_a, 3) + ",";
      json += "\"power_w\":" + String(st.power_w, 1) + ",";
      json += "\"bus_low\":" + String(st.bus_v < PSU_LOW_VOLTAGE_V ? "true" : "false") + ",";
    }
    // Energy since midnight; kept while the meter is briefly silent.
    json += "\"energy_kwh\":" + String(st.energy_wh / 1000.0f, 3) + ",";
    json += "\"heater_active\":" + String(st.heater_active ? "true" : "false") + ",";
    json += "\"heater_today_s\":" + String((unsigned long)st.heater_on_s); // Наработка за сутки платы
    json += "}";
    request->send(200, "application/json", json);
  });

  // Отдаёт содержимое кольцевого буфера Serial.print()-сообщений как обычный текст —
  // без JSON-обёртки, чтобы не думать про экранирование кавычек/переводов строк в логах.
  // Названо /api/console, а не /api/log — последний уже занят историей CSV с SD-карты для графика.
  server.on("/api/console", HTTP_GET, [](AsyncWebServerRequest *request){
    request->send(200, "text/plain; charset=utf-8", getLogSnapshot());
  });

  server.on("/api/settings", HTTP_GET, [](AsyncWebServerRequest *request){
    Settings cfg = settingsSnapshot();
    DateTime now = getWebDateTime();
    char buf[32]; snprintf(buf, sizeof(buf), "%02d.%02d.%04d %02d:%02d:%02d", now.day(), now.month(), now.year(), now.hour(), now.minute(), now.second());

    String json = "{";
    appendSettingsJson(json, cfg);
    json += "\"csrf_token\":\"" + csrf_token + "\",";
    json += "\"rtc_time\":\"" + String(buf) + "\",";

    uint64_t sdTotal = SD.totalBytes();
    uint64_t sdUsed  = SD.usedBytes();
    uint64_t sdFree  = (sdTotal > sdUsed) ? (sdTotal - sdUsed) : 0;
    json += "\"sdTotal\":" + u64str(sdTotal) + ",";
    json += "\"sdUsed\":"  + u64str(sdUsed)  + ",";
    json += "\"sdFree\":"  + u64str(sdFree);
    json += "}";
    request->send(200, "application/json", json);
  });

  server.on("/save-settings", HTTP_POST, [](AsyncWebServerRequest *request){
    // Работаем с копией: разбираем форму, проверяем, пишем в NVS и только потом публикуем
    // целиком — loop() и задача Wi-Fi никогда не видят наполовину применённые настройки.
    // Обработчики AsyncWebServer выполняются по одному, так что два сохранения
    // одновременно не случаются и изменения друг друга не затирают.
    Settings prev = settingsSnapshot();
    Settings next = prev;
    String rejected = applyFormToSettings(request, next);

    // Дата начала цикла приходит строкой YYYY-MM-DD — в таблицу не укладывается.
    // Пустое поле сбрасывает цикл в "не начат" (раньше оно вело на голую страницу с ошибкой 400).
    if (request->hasParam("start_date", true)) {
      String startDate = request->getParam("start_date", true)->value();
      startDate.trim();
      int y, m, d;
      if (startDate.length() == 0) {
        next.start_timestamp = 0;
      } else if (startDate.length() == 10 && parseDate(startDate, y, m, d)) {
        next.start_timestamp = DateTime(y, m, d, 0, 0, 0).unixtime();
      } else {
        addErrorGroup(rejected, "date");
      }
    }

    saveChangedSettings(prev, next);
    publishSettings(next);

    // Новые данные роутера — будим фоновую задачу, чтобы она не ждала до двух минут
    // в своём сне и переподключилась сразу после сохранения.
    if (WifiTaskHandle != NULL &&
        (strcmp(prev.wifi_ssid, next.wifi_ssid) != 0 ||
         strcmp(prev.wifi_pass, next.wifi_pass) != 0)) {
      xTaskNotifyGive(WifiTaskHandle);
    }
    // Новые адрес, токен или имя платы — отправляем сразу, не дожидаясь следующей минуты.
    if (TelemetryTaskHandle != NULL &&
        (strcmp(prev.telemetry_url, next.telemetry_url) != 0 ||
         strcmp(prev.telemetry_token, next.telemetry_token) != 0 ||
         strcmp(prev.device_label, next.device_label) != 0)) {
      xTaskNotifyGive(TelemetryTaskHandle);
    }

    // Новые SSID/пароль точки доступа применяем сразу, без перезагрузки платы.
    // Текущие клиенты при этом отключатся и должны подключиться заново с новыми данными.
    if (strcmp(prev.ap_ssid, next.ap_ssid) != 0 || strcmp(prev.ap_pass, next.ap_pass) != 0) {
      WiFi.softAP(next.ap_ssid, next.ap_pass[0] ? next.ap_pass : NULL);
    }

    // saved=1 — страница показывает "Настройки сохранены" и остаётся на той же вкладке
    if (rejected.length()) request->redirect("/settings?error=" + rejected);
    else request->redirect("/settings?saved=1");
  });

  server.on("/set-time", HTTP_POST, [](AsyncWebServerRequest *request){
    if (request->hasParam("datetime", true)) {
      DateTime userTime((uint32_t)0);
      if (!parseDateTimeLocal(request->getParam("datetime", true)->value(), userTime)) {
        request->redirect("/settings?error=time");
        return;
      }
      // Само применение — в loop(): rtcAdjust() из задачи веб-сервера означал бы
      // обращение к I2C с другого ядра, параллельно с опросом SHT4x.
      // Итог (pending -> ok/failed) страница настроек читает из /api/status.
      time_set_result = TIME_SET_PENDING;
      pending_time_set = userTime.unixtime();
      request->redirect("/settings?time=1"); // Страница сама дождётся итога записи
      return;
    }
    request->redirect("/settings?saved=1");
  });

  server.on("/api/log", HTTP_GET, [](AsyncWebServerRequest *request){
      String logDate = request->hasParam("date") ? request->getParam("date")->value() : "";
      if (logDate.length() > 0 && !isValidLogDate(logDate)) {
        request->send(400, "text/plain", "Bad date");
        return;
      }
      if (logDate == "") {
        DateTime now = getWebDateTime();
        char buf[16];
        snprintf(buf, sizeof(buf), "%04d-%02d-%02d", now.year(), now.month(), now.day());
        logDate = String(buf);
      }
      String filename = "/" + logDate + ".csv";
      if (SD.exists(filename)) {
        request->send(SD, filename, "text/csv"); // Убедись, что тип text/csv
      } else {
        request->send(200, "text/plain", "timestamp;temp;hum;led;fan1;fan2\n");
      }
  });

  // Список последних файлов в корне SD (для вкладки "Общее" на странице настроек)
  server.on("/api/files", HTTP_GET, [](AsyncWebServerRequest *request){
    std::vector<String> names;
    std::vector<uint32_t> sizes;

    File root = SD.open("/");
    if (root && root.isDirectory()) {
      File entry = root.openNextFile();
      while (entry) {
        if (!entry.isDirectory()) {
          String n = String(entry.name());
          int slash = n.lastIndexOf('/');
          if (slash >= 0) n = n.substring(slash + 1); // только имя, без пути
          names.push_back(n);
          sizes.push_back((uint32_t)entry.size());
        }
        entry.close();
        entry = root.openNextFile();
      }
      root.close();
    }

    // Файлы датированы YYYY-MM-DD.csv — идут по возрастанию, значит "последние" = конец списка
    int total = names.size();
    int start = total > 10 ? total - 10 : 0;

    String json = "{\"total\":" + String(total) + ",\"files\":[";
    for (int i = start; i < total; i++) {
      if (i > start) json += ",";
      json += "{\"name\":\"" + jsonEscape(names[i].c_str()) + "\",\"size\":" + String(sizes[i]) + "}";
    }
    json += "]}";
    request->send(200, "application/json", json);
  });

  // Удаление логов: именно POST. GET-версию можно было выстрелить обычным <img src="...">
  // с любой открытой страницы, пока телефон подключён к точке доступа, — и карта стиралась.
  server.on("/api/clearlogs", HTTP_POST, [](AsyncWebServerRequest *request){
    int removed = clearCsvLogs();
    logPrintln("Очистка логов SD: удалено файлов " + String(removed));
    request->send(200, "text/plain", "OK");
  });

  // Статика регистрируется последней: так обработчик "/" гарантированно не встанет
  // перед /api/* ни в одной из версий ESPAsyncWebServer
  server.serveStatic("/", SPIFFS, "/").setCacheControl("no-store");
  server.onNotFound([](AsyncWebServerRequest *request){ request->send(404, "text/plain", "Not found"); });

  server.begin();
}

void loop() {
  static unsigned long lastClimateTime = 0;
  unsigned long currentMillis = millis();
  stopPumpAtDeadline();
  tryRecoverRTC(currentMillis); // Не блокирует: сам ограничивает частоту попыток внутри
  debounceUpdate(water_level, isWaterAvailable(), currentMillis, WATER_DEBOUNCE_MS);

  // Время читаем раз в секунду, а не на каждой итерации loop(): rtc.now() — полноценная
  // транзакция по I2C, и раньше она крутилась тысячи раз в секунду на той же шине,
  // по которой опрашивается SHT4x. Там же обновляем свою копию настроек: изменения из
  // веба вступают в силу не позже чем через секунду, а весь цикл видит одну цельную версию.
  static DateTime now((uint32_t)0);
  static Settings cfg;
  static bool cfgLoaded = false;
  static unsigned long lastTimeRead = 0;
  if (lastTimeRead == 0 || currentMillis - lastTimeRead >= 1000) {
    lastTimeRead = currentMillis;
    now = getSafeDateTime();
    state.clock_usable = state.clock_trusted || backup_clock_synced;
    cached_unixtime = now.unixtime(); // Снимок, который отдают веб-обработчики

    Settings fresh = settingsSnapshot();
    // Пользователь поменял расписание полива или режим: с этого момента расписание действует
    // заново, а время, прошедшее сегодня раньше, не догоняется (см. watering_armed_from).
    if (cfgLoaded && (fresh.operation_mode != cfg.operation_mode || fresh.watering_days != cfg.watering_days ||
                      fresh.watering_hour != cfg.watering_hour || fresh.watering_minute != cfg.watering_minute)) {
      watering_armed_from = now.unixtime();
    }
    cfg = fresh;
    cfgLoaded = true;

    if (cfg.operation_mode == OP_MODE_DRYING) {
      // Drying всегда выключает полив и свет, даже если они были активны при переключении.
      if (state.pump_active) {
        state.pump_active = false;
        digitalWrite(PUMP_PIN, LOW);
        logPrintln("[Drying] Полив остановлен при выборе режима сушки.");
      }
      if (state.led_pwm != 0) {
        state.led_pwm = 0;
        ledcWrite(LED_PWM_PIN, 0);
      }
    }
  }
  // Режим берётся только из cfg — той же копии настроек, по которой считается весь цикл.
  // Раньше рядом жил отдельный атомарный флаг, и до секунды после переключения они расходились.
  const bool drying_mode = cfg.operation_mode == OP_MODE_DRYING;

  // Часы, выставленные из веб-формы, применяем здесь: в I2C ходит только loop()
  uint32_t requested = pending_time_set.exchange(0);
  if (requested != 0) {
    DateTime userTime(requested);
    state.rtc_online = false;
    if (rtcBegin() && rtcAdjust(userTime)) {
      DateTime checked;
      state.rtc_online = readRTC(checked) && checked.unixtime() >= requested && checked.unixtime() - requested <= 2;
      if (!state.rtc_online) logPrintln("[RTC] Установка: часы пошли, но проверочное чтение не совпало со временем.");
    }
    if (!state.rtc_online) {
      // Stop the clock, so tryRecoverRTC() cannot later trust a half-written or unverified time.
      const uint8_t stopped = 0;
      if (rtc_present && writeRtcRegisters(0x00, &stopped, 1)) state.rtc_status = RTC_NOT_SET;
    }
    state.clock_trusted = state.rtc_online;
    time_set_result = state.rtc_online ? TIME_SET_OK : TIME_SET_FAILED;
    rtc_recovery_streak = 0;
    backup_unixtime = requested;
    last_rtc_check_ms = millis();
    backup_clock_synced = true; // Владелец ввёл время: программный таймер идёт от него
    state.clock_usable = true;
    now = userTime;
    cached_unixtime = requested;
    watering_armed_from = requested; // Полив, "пропущенный" по новым часам, не догоняем
    logPrintln(state.rtc_online ? "[RTC] MCP7940N: время записано и проверено."
                               : "[RTC] Установка в RTC не удалась, часы остановлены; время идёт по программному таймеру "
                                 "до перезагрузки, полив работает по нему. Проверьте RTC и повторите установку.");
  }
  // An untrusted clock only blocks new waterings. A running one still ends by millis()
  // (stopPumpAtDeadline), so a brief RTC glitch no longer costs the day's watering.

  updatePowerMeter(currentMillis, now.unixtime() / 86400, cfg.power_cal_pct); // Self-limits to 1 s
  const bool freshPower = state.power_ok && power_last_sample_ms != 0 &&
                          elapsedMs(currentMillis, power_last_sample_ms) <= POWER_FRESH_MS;
  led_supply_allowed = ledSupplyAllowed(led_supply_allowed, freshPower, state.bus_v, power_online,
                                        state.clock_usable);
  state.led_supply_blocked = !led_supply_allowed;
  // Lite has no hardware UVLO/PGOOD gate. Apply cutoff every loop, outside the 10 s climate update.
  if (!led_supply_allowed && state.led_pwm != 0) {
    state.led_pwm = 0;
    ledcWrite(LED_PWM_PIN, 0);
  }
  // Измеритель молчит, а свет разрешён по часам и датчику: защиты от просадки питания нет. Пишем
  // об этом один раз, если так длится дольше 30 с, чтобы одиночный сбой не засорял журнал.
  static uint32_t unmeasuredSinceMs = 0;
  static bool unmeasuredReported = false;
  if (led_supply_allowed && !freshPower && !power_online) {
    if (unmeasuredSinceMs == 0) unmeasuredSinceMs = currentMillis ? currentMillis : 1;
    if (!unmeasuredReported && elapsedMs(currentMillis, unmeasuredSinceMs) >= 30000) {
      unmeasuredReported = true;
      logPrintln("[Power] Нет связи с измерителем питания (INA238): свет работает без защиты от низкого напряжения.");
    }
  } else {
    unmeasuredSinceMs = 0;
    unmeasuredReported = false;
  }


  if (currentMillis - lastClimateTime >= 10000) {
    lastClimateTime = currentMillis;

    int current_hour = now.hour();
    int current_minutes = current_hour * 60 + now.minute();
    int on_minutes = cfg.led_on_hour * 60 + cfg.led_on_minute;
    int off_minutes = cfg.led_off_hour * 60 + cfg.led_off_minute;

    if (on_minutes == off_minutes) state.is_day = true; // время включения == времени выключения — считаем "весь день"
    else if (on_minutes < off_minutes) state.is_day = (current_minutes >= on_minutes && current_minutes < off_minutes);
    else state.is_day = (current_minutes >= on_minutes || current_minutes < off_minutes);
    const bool is_day = state.is_day;

    int pwm_fan1_min = map(cfg.fan1_min_limit, 0, 100, 0, 255);
    int pwm_fan1_max = map(cfg.fan1_max_limit, 0, 100, 0, 255);
    int pwm_fan2_min = map(cfg.fan2_min_limit, 0, 100, 0, 255);
    int pwm_fan2_max = map(cfg.fan2_max_limit, 0, 100, 0, 255);
    int pwm_led_min = map(cfg.led_min_limit, 0, 100, 0, 255);
    int pwm_led_max = map(cfg.led_max_limit, 0, 100, 0, 255);
    int target_led, target_fan1, target_fan2;
    float read_temp = state.temp; // По умолчанию — последнее известное валидное значение, а не 0.0
    float read_hum = state.hum;

    // Чтение датчика с проверкой на ошибку
    sensors_event_t humidity, temp;
    if (state.sht_online) {
      if (sht40.getEvent(&humidity, &temp)) {
        read_temp = temp.temperature;
        read_hum = humidity.relative_humidity;

        // Защита от "зависших" нереалистичных данных (за пределами работы датчика)
        if (read_temp < -20.0 || read_temp > 80.0 || read_hum < 0.0 || read_hum > 100.0) {
          state.sht_online = false;
          logPrintf("[SHT4x] Показания вне рабочего диапазона: T=%.2f RH=%.2f\n", read_temp, read_hum);
        }
      } else {
        state.sht_online = false;
        logPrintln("[SHT4x] Датчик перестал отвечать (getEvent вернул ошибку).");
      }
      if (!state.sht_online) {
        // Причину пишем один раз, в момент отказа, — дальше её подтверждают попытки восстановления
        logSht4xDiagnosis();
        logI2CScan();
      }
    }

    // Датчик офлайн — периодически пробуем восстановиться, не дожидаясь перезагрузки платы.
    // Нужно несколько (SHT_RECOVERY_STREAK_NEEDED) подряд успешных попыток с интервалом
    // SHT_RETRY_INTERVAL_MS, прежде чем снова начать доверять показаниям.
    if (!state.sht_online && (currentMillis - last_sht_retry >= SHT_RETRY_INTERVAL_MS)) {
      last_sht_retry = currentMillis;
      recoverI2CBus(); // Освобождаем шину на случай, если она физически "залипла"
      sensors_event_t retryHumidity, retryTemp;
      bool retry_ok = sht40.begin() && sht40.getEvent(&retryHumidity, &retryTemp) &&
                       retryTemp.temperature >= -20.0 && retryTemp.temperature <= 80.0 &&
                       retryHumidity.relative_humidity >= 0.0 && retryHumidity.relative_humidity <= 100.0;

      if (retry_ok) {
        sht_recovery_streak++;
        logPrintf("[SHT4x] Попытка восстановления %d/%d успешна\n", sht_recovery_streak, SHT_RECOVERY_STREAK_NEEDED);
        if (sht_recovery_streak >= SHT_RECOVERY_STREAK_NEEDED) {
          state.sht_online = true;
          read_temp = retryTemp.temperature;
          read_hum = retryHumidity.relative_humidity;
          sht_recovery_streak = 0;
          last_emergency_log_ms = 0; // Следующий отказ снова будет залогирован сразу
          logPrintln("[SHT4x] Датчик восстановлен, выходим из аварийного режима.");
        }
      } else {
        sht_recovery_streak = 0; // Сбрасываем счётчик серии при любой неудачной попытке
        logSht4xDiagnosis();     // Раз в 30 секунд — на каком шаге датчик по-прежнему не отвечает
      }
    }

    // ЛОГИКА АВАРИЙНОГО РЕЖИМА ИЛИ НОРМАЛЬНОЙ РАБОТЫ
    if (!state.sht_online) {
      // --- АВАРИЯ: Датчик сломан. Включаем безопасный пресет ---
      // При отказе датчика держим вентиляторы на середине их настроенного диапазона.
      target_fan1 = map(50, 0, 100, pwm_fan1_min, pwm_fan1_max);
      target_fan2 = map(50, 0, 100, pwm_fan2_min, pwm_fan2_max);
      // Светильник на минимальный уровень дня, чтобы не сжечь растения светом/жаром
      target_led = is_day ? pwm_led_min : 0;

      // Датчик недоступен — не доверяем показаниям, обогрев выключаем из соображений безопасности
      state.heater_active = false;
      digitalWrite(HEATER_PIN, LOW);

      // Раньше эта строка писалась каждые 10 секунд и за пару минут вытесняла из 4-килобайтной
      // веб-консоли всё остальное — включая сообщения о том, почему датчик отказал
      if (last_emergency_log_ms == 0 || currentMillis - last_emergency_log_ms >= 300000) {
        last_emergency_log_ms = currentMillis;
        logPrintln("[АВАРИЙНЫЙ РЕЖИМ]: Отказ SHT4x! Климат зафиксирован на безопасных уровнях.");
      }
    }
    else {
      // --- НОРМАЛЬНАЯ РАБОТА ---
      // Защита от temp_delta == 0, чтобы map() не делил на ноль
      float safe_delta = max(cfg.temp_delta, 0.1f);
      float current_min = cfg.temp_target - safe_delta;
      float current_max = cfg.temp_target + safe_delta;

      float heater_delta = drying_mode ? max(cfg.drying_temp_delta, 0.1f) : safe_delta;
      // В режиме Growing сохраняется прежний дневной/ночной термостат. В Drying
      // нагреватель независимо от расписания поддерживает отдельную температурную цель.
      float heater_target = drying_mode ? cfg.drying_temp_target : (is_day ? cfg.temp_target : cfg.temp_target_night);
      float heater_min = heater_target - heater_delta;
      float heater_off_threshold = drying_mode ? heater_target + heater_delta : heater_target;

      // "Никогда" (3) запрещает обогрев в любом режиме: раньше Drying этот выбор игнорировал,
      // и отключить нагреватель на время сушки было нечем. Расписание день/ночь в Drying
      // не действует — там нет смены дня и ночи.
      bool heater_schedule_ok = drying_mode ? cfg.heater_mode != 3
                                            : (cfg.heater_mode == 2 || (cfg.heater_mode == 0 && is_day) ||
                                               (cfg.heater_mode == 1 && !is_day));
      // heaterNextState() (Safety.h) also switches off on a NaN reading or threshold and at
      // HEATER_HARD_LIMIT_C, whatever the settings say.
      const bool was_heating = state.heater_active;
      state.heater_active = heaterNextState(state.heater_active, heater_schedule_ok, read_temp, heater_min,
                                            heater_off_threshold, HEATER_HARD_LIMIT_C);
      if (was_heating && !state.heater_active && finiteFloat(read_temp) && read_temp >= HEATER_HARD_LIMIT_C) {
        logPrintln("[Heater] Off: temperature reached the " + String(HEATER_HARD_LIMIT_C, 0) + " C hard limit.");
      }
      digitalWrite(HEATER_PIN, state.heater_active ? HIGH : LOW);

      if (drying_mode) {
        // При сушке свет не используется. По влажности выше заданной плавно
        // увеличиваем скорость вентиляторов: от цели RH до цели + диапазон.
        target_led = 0;
        float humidity_min = cfg.drying_hum_target;
        float humidity_max = min(100.0f, humidity_min + max(cfg.drying_hum_band, 0.1f));
        // Сброс тепла вентиляторами — на два гистерезиса выше цели. Раньше порог совпадал с
        // порогом выключения нагревателя (цель + гистерезис), и каждый цикл нагрева кончался
        // тем, что вентиляторы тут же выдували только что набранное тепло.
        float vent_threshold = cfg.drying_temp_target + 2.0f * heater_delta;
        if (read_temp >= vent_threshold ||
            (humidity_max > humidity_min && read_hum >= humidity_max)) {
          target_fan1 = pwm_fan1_max;
          target_fan2 = pwm_fan2_max;
        } else if (humidity_max <= humidity_min || read_hum <= humidity_min) {
          target_fan1 = pwm_fan1_min;
          target_fan2 = pwm_fan2_min;
        } else {
          target_fan1 = map(read_hum * 100, humidity_min * 100, humidity_max * 100, pwm_fan1_min, pwm_fan1_max);
          target_fan2 = map(read_hum * 100, humidity_min * 100, humidity_max * 100, pwm_fan2_min, pwm_fan2_max);
        }
      } else {
        // В Growing вентиляторы используют ту же температурную цель, что и обогрев:
        // дневную при включённом свете и ночную при выключённом.
        float fan_target = is_day ? cfg.temp_target : cfg.temp_target_night;
        float fan_min_temp = fan_target - safe_delta;
        float fan_max_temp = fan_target + safe_delta;

        if (read_temp <= current_min) {
          target_led = is_day ? pwm_led_max : 0;
        }
        else if (read_temp >= current_max) {
          target_led = is_day ? pwm_led_min : 0;
        }
        else {
          target_led = is_day ? map(read_temp * 100, current_min * 100, current_max * 100, pwm_led_max, pwm_led_min) : 0;
        }

        if (read_temp <= fan_min_temp) {
          target_fan1 = pwm_fan1_min;
          target_fan2 = pwm_fan2_min;
        } else if (read_temp >= fan_max_temp) {
          target_fan1 = pwm_fan1_max;
          target_fan2 = pwm_fan2_max;
        } else {
          target_fan1 = map(read_temp * 100, fan_min_temp * 100, fan_max_temp * 100, pwm_fan1_min, pwm_fan1_max);
          target_fan2 = map(read_temp * 100, fan_min_temp * 100, fan_max_temp * 100, pwm_fan2_min, pwm_fan2_max);
        }

        // Пока обогреватель греет, вентиляторы держим на минимуме. Иначе на отрезке
        // "цель − дельта … цель" они разгонялись до половины диапазона и выдували тепло,
        // которое обогреватель производит: слабый обогреватель мог не выключиться никогда.
        if (state.heater_active) {
          target_fan1 = pwm_fan1_min;
          target_fan2 = pwm_fan2_min;
        }
      }
    }

    if (drying_mode) target_led = 0; // В сушке свет не включаем и при отказе датчика
    // Страхуемся от выхода за пределы 8-битного ШИМ (map() с кривыми границами это умеет)
    target_led = led_supply_allowed ? ledDutyForRequest(target_led) : 0;
    target_fan1 = clampInt(target_fan1, 0, 255);
    target_fan2 = clampInt(target_fan2, 0, 255);

    // Храним только валидные показания: иначе значения вне диапазона (из-за которых датчик
    // и признан отказавшим) уезжали в CSV и становились "последним известным" для следующего цикла
    if (state.sht_online) {
      state.temp = read_temp;
      state.hum = read_hum;
    }
    state.led_pwm = target_led;
    state.fan1_pwm = target_fan1;
    state.fan2_pwm = target_fan2;

    ledcWrite(LED_PWM_PIN, target_led);
    ledcWrite(FAN1_PWM_PIN, fanGpioDuty(target_fan1));
    ledcWrite(FAN2_PWM_PIN, fanGpioDuty(target_fan2));
  }

  // Запись лога на SD-карту с флагами состояния
  static unsigned long lastLogBackupTime = 0;
  if (currentMillis - lastLogBackupTime >= 600000) {
    lastLogBackupTime = currentMillis;
    char logFilename[20];
    snprintf(logFilename, sizeof(logFilename), "/%04d-%02d-%02d.csv", now.year(), now.month(), now.day());

    const float avgPower = takeCsvAveragePower(); // Taken even without SD: each row covers 10 min
    bool fileExists = SD.exists(logFilename);
    File logFile = SD.open(logFilename, FILE_APPEND);
    if (logFile) {
      // power_w is appended last, so older readers of the first 8 columns keep working.
      // It is the average over the 10-minute interval, empty when the INA238 gave nothing.
      if (!fileExists) logFile.println("timestamp;temp;hum;led;fan1;fan2;sht_ok;rtc_ok;power_w");
      logFile.print(now.timestamp(DateTime::TIMESTAMP_FULL)); logFile.print(";");
      logFile.print(state.temp, 2); logFile.print(";");
      logFile.print(state.hum, 2); logFile.print(";");
      logFile.print(state.led_pwm); logFile.print(";");
      logFile.print(state.fan1_pwm); logFile.print(";");
      logFile.print(state.fan2_pwm); logFile.print(";");
      logFile.print(state.sht_online ? "1" : "0"); logFile.print(";");
      logFile.print(state.rtc_online ? "1" : "0"); logFile.print(";");
      if (finiteFloat(avgPower)) logFile.print(avgPower, 1);
      logFile.println();
      logFile.close();
    }
  }

  // --- АВТОПОЛИВ ---
  static unsigned long lastWaterCheck = 0;
  if (currentMillis - lastWaterCheck >= 1000) {
    lastWaterCheck = currentMillis;

    // RTClib: dayOfTheWeek() возвращает 0=Вс...6=Сб. Переводим в формат watering_days: 0=Пн...6=Вс
    int rtc_dow = now.dayOfTheWeek();
    int iso_dow = (rtc_dow == 0) ? 6 : rtc_dow - 1;
    uint32_t today_code = now.unixtime() / 86400;

    // Отметка "последний полив" из будущего (часы когда-то стояли неверно, а потом их поправили)
    // раньше блокировала полив до той самой даты — хоть на год. Сдвигаем её на сегодня:
    // сегодня повтора не будет, с завтрашнего дня полив идёт как обычно.
    uint32_t reconciled = reconcileWateringMark(state.clock_usable, today_code, last_watering_day);
    if (reconciled != last_watering_day) {
      last_watering_day = reconciled;
      preferences.putUInt("last_water", reconciled);
      state.last_skip_day = reconciled; // The mark moved to today; nothing was watered today
      logPrintln("[ПОЛИВ] Отметка последнего полива была из будущего — сдвинута на сегодня.");
    }

    // Если датчик уровня воды выключен в настройках — считаем, что вода есть всегда (старое поведение)
    bool water_ok = !cfg.water_sensor_enabled || water_level.stable;
    state.water_present = water_level.stable;
    state.last_water_day = last_watering_day;

    if (drying_mode) {
      if (state.pump_active) {
        state.pump_active = false;
        digitalWrite(PUMP_PIN, LOW);
        logPrintln("[Drying] Полив остановлен при выборе режима сушки.");
      }
      digitalWrite(PUMP_PIN, LOW);
    } else if (!state.pump_active) {
      bool day_enabled = (cfg.watering_days >> iso_dow) & 0x01;
      // Раньше полив стартовал, только если текущие час и минута равны заданным: стоило плате
      // в эту минуту быть без питания или перезагружаться — и день оставался без полива.
      uint32_t scheduled = today_code * 86400UL + (uint32_t)cfg.watering_hour * 3600UL + (uint32_t)cfg.watering_minute * 60UL;
      if (day_enabled && wateringDue(now.unixtime(), scheduled, watering_armed_from, WATERING_CATCHUP_SEC) &&
          wateringDayAllowed(state.clock_usable, today_code, last_watering_day)) {
        uint32_t late_min = (now.unixtime() - scheduled) / 60;
        // Persist before energizing the pump; reboot during this minute must not repeat it.
        last_watering_day = today_code;
        if (preferences.putUInt("last_water", today_code) != sizeof(uint32_t)) {
          state.last_skip_day = today_code;
          logPrintln("[Watering] NVS write failed; pump remains off.");
        } else if (water_ok) {
          state.pump_active = true;
          pump_start_ms = millis();
          pump_duration_ms = uint32_t(cfg.watering_duration_sec) * 1000U;
          digitalWrite(PUMP_PIN, HIGH);
          // Через String, а не logPrintf: русская строка с пометкой длиннее его буфера в 192 байта.
          String startMsg = "[ПОЛИВ] Старт автополива";
          if (late_min > 0) startMsg += " с опозданием на " + String((unsigned)late_min) + " мин (плата была недоступна)";
          if (!state.clock_trusted) startMsg += " (время по программному таймеру, RTC не отвечает)";
          logPrintln(startMsg);
        } else {
          state.last_skip_day = today_code;
          logPrintln("[ПОЛИВ] Пропущен: датчик не видит воду в баке");
        }
      }
    } else {
      if (!water_ok) {
        // Аварийная остановка: вода закончилась прямо во время полива — не гоняем насос всухую
        state.pump_active = false;
        digitalWrite(PUMP_PIN, LOW);
        logPrintln("[ПОЛИВ] Аварийная остановка: вода закончилась во время полива");
      } else if (elapsedMs(millis(), pump_start_ms) >= pump_duration_ms) {
        // Длительность — по millis(), а не по RTC: если посреди полива восстановится RTC
        // (tryRecoverRTC) и время прыгнет, насос иначе либо выключится мгновенно, либо
        // будет лить, пока не сработает датчик уровня воды.
        state.pump_active = false;
        digitalWrite(PUMP_PIN, LOW);
        logPrintln("[ПОЛИВ] Полив завершён");
      }
    }
  }

  updateHeaterRuntime(millis(), now.unixtime() / 86400);
  updateStatusLed(cfg);
  publishState(); // Сравнение с опубликованной копией; если ничего не менялось — no-op
  esp_task_wdt_reset();
  delay(1); // Yield to the monitored idle task.
}
