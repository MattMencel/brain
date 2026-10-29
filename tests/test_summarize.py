from brain.summarize import clamp, number


def test_number_labels_each_turn_in_order():
    assert number("USER: a\n\nCLAUDE: b\n\nUSER: c") == "[1] USER: a\n\n[2] CLAUDE: b\n\n[3] USER: c"


def test_clamp_keeps_ranges_inside_transcript_and_ordered():
    topics = clamp([{"turn_from": 0, "turn_to": 99}, {"turn_from": 7, "turn_to": 3}], last=5)
    assert [(t["turn_from"], t["turn_to"]) for t in topics] == [(1, 5), (5, 5)]
