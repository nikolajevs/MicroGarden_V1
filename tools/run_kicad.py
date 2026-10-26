"""Run a project script in KiCad Python, including isolated Windows runtimes."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True, type=Path, help='Python executable bundled with KiCad')
    parser.add_argument('--dependencies', type=Path, help='Directory containing Shapely 2 and gerbonara')
    parser.add_argument('script', type=Path)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.python.is_file() or not args.script.is_file():
        parser.error('Python executable and script must exist')
    if args.dependencies and not args.dependencies.is_dir():
        parser.error('Dependencies directory must exist')
    script = args.script.resolve()
    paths = [str(script.parent)]
    if args.dependencies:
        paths.append(str(args.dependencies.resolve()))
    # Pass paths as arguments, never construct a shell command from filenames.
    bootstrap = ('import json,runpy,sys; sys.path[:0]=json.loads(sys.argv[1]); '
                 'path=sys.argv[2]; sys.argv=sys.argv[2:]; runpy.run_path(path,run_name="__main__")')
    import json
    result = subprocess.run([str(args.python.resolve()), '-B', '-X', 'utf8', '-c',
                             bootstrap, json.dumps(paths), str(script), *args.args])
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
