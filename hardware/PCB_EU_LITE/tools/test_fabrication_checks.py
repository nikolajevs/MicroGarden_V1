"""Regression tests for same-net Kelvin bridges that native DRC permits."""
import unittest
import pcbnew as p
from fabrication_checks import HERE, kelvin_check


class KelvinTests(unittest.TestCase):
    def board(self):
        return p.LoadBoard(str(HERE/'PCB_EU_LITE.kicad_pcb'))

    def test_current_board_has_isolated_sense_branches(self):
        self.assertEqual(set(kelvin_check(self.board())), {'IN+','IN-'})

    def test_old_address_pad_via_bridge_is_rejected(self):
        board=self.board()
        via=p.PCB_VIA(board)
        via.SetPosition(p.VECTOR2I(p.FromMM(18.3),p.FromMM(47)))
        via.SetWidth(p.FromMM(.5))
        via.SetDrill(p.FromMM(.3))
        via.SetLayerPair(p.F_Cu,p.B_Cu)
        via.SetNet(board.FindNet('GND'))
        board.Add(via)
        with self.assertRaisesRegex(RuntimeError, 'IN\\+'):
            kelvin_check(board)

    def test_minus_power_bridge_is_rejected(self):
        board=self.board()
        track=p.PCB_TRACK(board)
        track.SetStart(p.VECTOR2I(p.FromMM(19),p.FromMM(43)))
        track.SetEnd(p.VECTOR2I(p.FromMM(15.5),p.FromMM(47.92)))
        track.SetWidth(p.FromMM(.8))
        track.SetLayer(p.F_Cu)
        track.SetNet(board.FindNet('RTN_SHUNT'))
        board.Add(track)
        with self.assertRaisesRegex(RuntimeError, 'IN-'):
            kelvin_check(board)


if __name__=='__main__':
    unittest.main()
