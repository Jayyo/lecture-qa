from backend import qa

SEGMENTS = [
    {'start': 0.0, 'end': 9.0, 'text': 'Today: binary search.'},
    {'start': 9.0, 'end': 25.1, 'text': 'Linear search compares one by one.'},
    {'start': 25.1, 'end': 44.8, 'text': 'Compare with the middle and drop half.'},
    {'start': 44.8, 'end': 62.5, 'text': 'Example: find 42.'},
]


def test_fmt_time():
    assert qa.fmt_time(0) == '00:00'
    assert qa.fmt_time(75.9) == '01:15'
    assert qa.fmt_time(3725) == '1:02:05'


def test_build_input_marks_the_pause_after_the_current_segment():
    prompt = qa.build_input(SEGMENTS, 30, 'Why the middle?', title='Week 3')
    lines = prompt.splitlines()
    assert lines[0] == 'Lecture title: Week 3'
    middle = lines.index('[00:25] Compare with the middle and drop half.')
    assert lines[middle + 1] == '>>> the student paused here, at 00:30 <<<'
    assert lines[-1] == 'Question: Why the middle?'


def test_build_input_pause_after_last_segment_and_history():
    history = [{'question': 'What is n?', 'answer': 'The array size [00:09].'}]
    prompt = qa.build_input(SEGMENTS, 70, 'And log n?', history=history)
    assert '[00:44] Example: find 42.\n>>> the student paused here, at 01:10 <<<' in prompt
    assert 'Student: What is n?\nAssistant: The array size [00:09].' in prompt


def test_transcript_window_keeps_everything_when_it_fits():
    assert qa.transcript_window(SEGMENTS, 30, 10_000) == SEGMENTS


def test_transcript_window_centers_on_the_pause():
    segments = [{'start': i * 10.0, 'end': i * 10.0 + 10, 'text': 'x' * 90} for i in range(100)]
    window = qa.transcript_window(segments, 505, 1000)
    starts = [s['start'] for s in window]
    assert 500.0 in starts
    assert sum(len(s['text']) + 10 for s in window) <= 1000
    assert starts == sorted(starts)
    # leans towards what was already said
    assert sum(1 for t in starts if t < 500) > sum(1 for t in starts if t > 500)
