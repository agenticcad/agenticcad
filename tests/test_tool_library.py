"""The tool library: an update by number keeps what it doesn't mention."""
from cam_data import Library


def test_updating_a_tool_keeps_its_photo_tip_and_shank(tmp_path):
    """Found filming the 1.0 tour: the agent saved T2/T6 with new feeds and their packet photos were gone - save_tool
    replaced the whole tool, so every field not sent (photo, tip, shank, stickout...) went back to its default."""
    L = Library(tmp_path)
    L.save_tool({"number": 6, "name": "0.3 mm 30° engraving bit", "type": "vbit", "diameter": 3.175, "angle": 30,
                 "tip_diameter": 0.3, "shank_diameter": 3.175, "stickout": 16, "photo": "abc.jpg", "rpm": 12000})
    L.save_tool({"number": 6, "name": "0.3 mm 30° engraving bit", "rpm": 13000, "feed": 120})
    t = L.tool_map()[6]
    assert (t.rpm, t.feed, t.photo, t.tip_diameter, t.angle, t.type, t.stickout) == (13000, 120, "abc.jpg", 0.3, 30, "vbit", 16)
    # explicit values still clear (the Settings editor sends '' / 0 to remove a photo or a tip)
    L.save_tool({"number": 6, "name": "0.3 mm 30° engraving bit", "photo": "", "tip_diameter": 0})
    t = L.tool_map()[6]
    assert t.photo == "" and t.tip_diameter == 0
    # a new number is a new tool from its own fields
    L.save_tool({"number": 7, "name": "new", "diameter": 2})
    assert L.tool_map()[7].photo in ("", None) and L.tool_map()[7].diameter == 2
