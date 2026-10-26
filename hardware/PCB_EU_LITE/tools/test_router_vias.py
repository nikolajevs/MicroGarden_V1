"""A through via must clear inner tracks even in an outer-layer-only search."""

import unittest
import pcbnew as p
import route_complete as router


class ThroughViaClearance(unittest.TestCase):
    def test_outer_layer_search_respects_inner_copper(self):
        board = p.BOARD()
        board.SetCopperLayerCount(4)
        for a, b in (
            ((0, 0), (20, 0)),
            ((20, 0), (20, 20)),
            ((20, 20), (0, 20)),
            ((0, 20), (0, 0)),
        ):
            edge = p.PCB_SHAPE(board)
            edge.SetShape(p.SHAPE_T_SEGMENT)
            edge.SetLayer(p.Edge_Cuts)
            edge.SetStart(p.VECTOR2I(*[p.FromMM(x) for x in a]))
            edge.SetEnd(p.VECTOR2I(*[p.FromMM(x) for x in b]))
            edge.SetWidth(p.FromMM(0.05))
            board.Add(edge)
        own = p.NETINFO_ITEM(board, "OUTER_POWER", 1)
        other = p.NETINFO_ITEM(board, "INNER_SIGNAL", 2)
        board.Add(own)
        board.Add(other)
        trace = p.PCB_TRACK(board)
        trace.SetNet(other)
        trace.SetLayer(p.In2_Cu)
        trace.SetWidth(p.FromMM(0.4))
        trace.SetStart(p.VECTOR2I(p.FromMM(2), p.FromMM(10)))
        trace.SetEnd(p.VECTOR2I(p.FromMM(18), p.FromMM(10)))
        board.Add(trace)
        old_layers, old_via = router.LAYERS, router.VIA
        try:
            router.LAYERS, router.VIA = [p.F_Cu, p.B_Cu], (0.8, 0.4)
            grid = router.Grid(
                board, own.GetNetCode(), (5, 5, 15, 15), 0.8, preserve_escapes=False
            )
            i, j = grid.cell(10, 10)
            self.assertTrue(grid.free(p.F_Cu, i, j))
            self.assertTrue(grid.free(p.B_Cu, i, j))
            self.assertEqual(grid.via[i * grid.ny + j], 1)
            i, j = grid.cell(10, 12)
            self.assertEqual(grid.via[i * grid.ny + j], 0)
        finally:
            router.LAYERS, router.VIA = old_layers, old_via


if __name__ == "__main__":
    unittest.main()
