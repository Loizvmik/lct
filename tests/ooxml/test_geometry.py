from deckforge.ooxml.geometry import Canvas, GroupFrame, resolve_point

CANVAS = Canvas(width_emu=12192000, height_emu=6858000)

def test_child_of_group_is_mapped_into_slide_coordinates():
    frame = GroupFrame(off=(282380, 3403462), ext=(1195387, 99124),
                       ch_off=(658813, 5548708), ch_ext=(2283170, 189326))
    # ребёнок стоит ровно в начале детской системы координат
    x, y = resolve_point(658813, 5548708, [frame])
    assert (x, y) == (282380, 3403462)

def test_child_at_far_corner_of_group_lands_on_group_edge():
    frame = GroupFrame(off=(282380, 3403462), ext=(1195387, 99124),
                       ch_off=(658813, 5548708), ch_ext=(2283170, 189326))
    x, y = resolve_point(658813 + 2283170, 5548708 + 189326, [frame])
    assert abs(x - (282380 + 1195387)) <= 1
    assert abs(y - (3403462 + 99124)) <= 1

def test_nested_groups_compose():
    outer = GroupFrame(off=(0, 0), ext=(1000, 1000), ch_off=(0, 0), ch_ext=(2000, 2000))
    inner = GroupFrame(off=(1000, 1000), ext=(1000, 1000), ch_off=(0, 0), ch_ext=(1000, 1000))
    # точка (500,500) внутри inner → (1500,1500) в системе outer → (750,750) в слайде
    assert resolve_point(500, 500, [outer, inner]) == (750, 750)

def test_zero_child_extent_does_not_divide_by_zero():
    frame = GroupFrame(off=(10, 10), ext=(100, 100), ch_off=(0, 0), ch_ext=(0, 0))
    assert resolve_point(50, 50, [frame]) == (10, 10)

def test_box_is_fraction_of_canvas():
    from deckforge.ooxml.geometry import Box, box_from_emu
    box = box_from_emu(left=6096000, top=0, width=6096000, height=6858000, canvas=CANVAS)
    assert box == Box(left=0.5, top=0.0, width=0.5, height=1.0)
    assert box.right == 1.0
