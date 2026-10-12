from autopilot.mirror import Mirror


def test_incremental_diff_updates_adds_and_removes(tmp_path):
    mirror = Mirror(tmp_path / 'mirror.json')
    mirror.apply({'serverTimestamp': 10, 'transaction': [{'id': 'a', 'income': 1}, {'id': 'b', 'income': 2}],
                  'tag': [{'id': 't'}]}, full=True)
    mirror.apply({'serverTimestamp': 20, 'transaction': [{'id': 'a', 'income': 5}, {'id': 'c', 'income': 3}],
                  'deletion': [{'id': 't', 'object': 'tag'}]})
    mirror.save()
    loaded = Mirror.load(tmp_path / 'mirror.json')
    snapshot = loaded.snapshot()
    assert loaded.server_timestamp == 20
    assert {t['id']: t['income'] for t in snapshot['transaction']} == {'a': 5, 'b': 2, 'c': 3}
    assert snapshot['tag'] == []


def test_broken_file_starts_from_a_full_download(tmp_path):
    (tmp_path / 'mirror.json').write_text('{not json')
    assert Mirror.load(tmp_path / 'mirror.json').needs_full(now=0)
