import json
import sqlite3

import pytest

from app import store
from app.errors import DomainError
from app.workflow import load, run

CLEAN = load('clean.csv')
B1 = '2026-11-16T01:00:00+00:00'
B2 = '2026-11-16T02:00:00+00:00'


def window(**over):
    base = {'from': B1, 'to': '2026-11-16T02:59:59+00:00', 'regions': ['A', 'B', 'C']}
    base.update(over)
    return base


def confirmed(offset=2, **over):
    # 覆盖全部区域：维护窗口内的每条观测都必须有校准依据，否则整段隔离。
    record = {'id': 'CAL-A', 'basis': '维护后探头A新校准', 'status': 'confirmed',
              'offset': offset}
    record.update(over)
    return record


def carried(offset=2, **over):
    record = dict(confirmed(offset), id='CAL-H', basis='维护前历史校准', status='carried',
                  source='historical')
    record.update(over)
    return record


def obs(state, bucket, left, kind='zone'):
    return next(r for r in state['observations']
                if r['bucket'] == bucket and r['left'] == left and r['kind'] == kind)


# --- 裁决：哪些观测进入正式区间 -------------------------------------------

def test_confirmed_calibration_enters_formal_interval():
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed()]})
    row = obs(state, B1, 'A')
    assert row['calibration']['status'] == 'confirmed'
    assert (row['calibration']['before'], row['calibration']['after']) == (120, 122)
    assert B1 in state['business']['certified_buckets']
    assert state['business']['carried_buckets'] == []


def test_carried_calibration_enters_formal_interval_but_not_certified():
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [carried()]})
    assert B1 in state['business']['carried_buckets']
    assert B1 not in state['business']['certified_buckets']
    assert state['business']['carried_observations'] > 0
    # 正式区间仍包含未确认沿用，交得出当天客流
    assert state['business']['total'] > 0
    assert state['business']['certified_total'] == 0


def test_observation_without_calibration_is_isolated_not_silently_dropped():
    # B、C 区域仍在维护窗口但尚未补齐校准：整个时段都不能进入正式区间。
    state = run(CLEAN, {'maintenance': window(),
                        'calibrations': [confirmed(regions=['A'])]})
    assert set(state['calibration']['withheld_buckets']) == {B1, B2}
    codes = [q['code'] for q in state['quarantine']]
    assert 'CALIBRATION' in codes
    where = [q for q in state['quarantine'] if q['bucket'] == B1]
    # 整段隔离的每条观测都保留来源行、原值与校准依据
    assert len(where) == 6 and all(q['raw'] and q['line'] >= 2 for q in where)
    assert all(q['calibration']['basis'] for q in where)


def test_partial_bucket_is_withheld_as_a_whole():
    # A 有校准、B 没有：不能只用 A 一侧拼出残缺区间
    state = run(CLEAN, {'maintenance': window(regions=['A', 'B']),
                        'calibrations': [confirmed(regions=['A'])]})
    assert set(state['calibration']['withheld_buckets']) == {B1, B2}
    assert all(c['code'] == 'WITHHELD' for c in state['calculations'][:2])
    assert all(c['lower'] is None for c in state['calculations'][:2])
    # 修正前区间仍给出，供核对漏计了多少
    assert state['calculations'][0]['before']['lower'] is not None


def test_no_maintenance_window_keeps_legacy_behaviour():
    state = run(CLEAN, {})
    assert state['calibration']['withheld_buckets'] == []
    assert {r['calibration']['status'] for r in state['observations']} == {'raw'}
    assert state['business']['total'] == state['reconcile']['business']['total']


# --- 冲突与重试 -------------------------------------------------------------

def test_same_id_different_content_conflicts_and_isolates():
    conflict = [confirmed(2), confirmed(3)]
    state = run(CLEAN, {'maintenance': window(), 'calibrations': conflict})
    assert state['calibration']['conflict_calibrations'] == ['CAL-A']
    assert {q['code'] for q in state['quarantine'] if q.get('calibration')} == {'CALIBRATION_CONFLICT'}
    assert any(a['event'] == 'conflict' for a in state['calibration']['audit'])


def test_identical_duplicate_calibration_applies_once():
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed(2), confirmed(2)]})
    row = obs(state, B1, 'A')
    assert row['count'] == 122 and row['count_before'] == 120
    assert [a['event'] for a in state['calibration']['audit']] == ['duplicate_ignored']


def test_confirmed_overrides_carried_for_same_observation():
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [carried(3), confirmed(2)]})
    row = obs(state, B1, 'A')
    assert row['count'] == 122 and row['calibration']['status'] == 'confirmed'


def test_retrying_m3_does_not_double_apply():
    from app.modules.m3_versions.rules import apply as m3
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed()]})
    first = obs(state, B1, 'A')['count']
    again = m3(json.loads(json.dumps(state)))
    assert obs(again, B1, 'A')['count'] == first == 122
    assert len(again['quarantine']) == len(state['quarantine'])
    assert len(again['observations']) == len(state['observations'])


def test_retry_after_withholding_does_not_lose_or_duplicate_records():
    from app.modules.m3_versions.rules import apply as m3
    withheld = run(CLEAN, {'maintenance': window(),
                           'calibrations': [confirmed(id='CAL-A', regions=['A'])]})
    retried = m3(json.loads(json.dumps(withheld)))
    # 只有维护窗口内的两个时段被隔离，其余时段照常采信
    assert len(retried['observations']) == 60
    assert len(retried['calibration_withheld']) == 12
    assert len(retried['quarantine']) == 12
    # 补齐校准后重试，同一批记录全部回到正式区间
    # 维护窗口只看区域人数；补齐 B、C 探头校准后整段回到正式区间。
    complete = dict(withheld['parameters'], calibrations=[
        confirmed(id='CAL-A', regions=['A']),
        confirmed(id='CAL-B', regions=['B'], offset=0, basis='B新校准'),
        confirmed(id='CAL-C', regions=['C'], offset=0, basis='C新校准')])
    completed = m3(json.loads(json.dumps(dict(withheld, parameters=complete))))
    assert len(completed['calibration_withheld']) == 0
    assert len(completed['observations']) == 72
    # 重叠观测不会被单探头口径连带修正
    for row in completed['observations']:
        if row['kind'] == 'pair':
            assert row['calibration']['status'] == 'raw'
            assert row['count'] == row['count_before']


# --- 只重算受影响时段 -------------------------------------------------------

def test_completing_calibration_reuses_unaffected_buckets():
    partial = run(CLEAN, {'maintenance': window(regions=['A']), 'calibrations': [confirmed()]})
    last = '2026-11-16T12:00:00+00:00'
    assert partial['recompute']['recomputed'][-1] == last
    optimised = run(CLEAN, {'maintenance': window(regions=['A']), 'calibrations': [confirmed()]},
                    previous=partial)
    assert optimised['recompute']['recomputed'] == []
    assert optimised['recompute']['reused'] == partial['recompute']['recomputed']
    assert optimised['business']['total'] == partial['business']['total']


def test_new_calibration_recomputes_only_the_filled_window():
    partial = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed(
        id='CAL-A1', bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00')]})
    complete = run(CLEAN, {'maintenance': window(), 'calibrations': [
        confirmed(id='CAL-A1', bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00'),
        confirmed(id='CAL-A2', bucket_from=B2, bucket_to='2026-11-16T02:59:59+00:00')]},
        previous=partial)
    assert B1 in complete['recompute']['reused']
    assert B2 in complete['recompute']['recomputed']
    assert B2 not in complete['recompute']['reused']


def test_m4_reuses_model_but_recomputes_changed_bucket():
    first = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed(
        id='CAL-1', bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00')]})
    second = run(CLEAN, {'maintenance': window(), 'calibrations': [
        confirmed(id='CAL-1', bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00'),
        confirmed(id='CAL-2', bucket_from=B2, bucket_to='2026-11-16T02:59:59+00:00')]},
        previous=first)
    reused = {m['bucket'] for m in second['models'] if m.get('reused')}
    assert B1 in reused and B2 not in reused
    assert B2 in second['recompute']['recomputed']
    assert second['business']['total'] != first['business']['total']


# --- 保存失败保留旧版 -------------------------------------------------------

class _FailingDB:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args):
        if 'INTO runs' in sql:
            raise sqlite3.OperationalError('disk full')
        raise AssertionError('保存失败时不应继续写入: ' + sql)


def test_save_failure_keeps_previous_batch(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed()]})
    store.save(state['run_id'], state)
    before = store.get(state['run_id'])
    assert store.latest() == state['run_id']

    updated = json.loads(json.dumps(state))
    updated['run_id'] = 'new-batch'
    original = store.connection
    monkeypatch.setattr(store, 'connection', lambda: _FailingDB())
    with pytest.raises(DomainError) as error:
        store.save('new-batch', updated)
    assert error.value.code == 'SAVE'
    monkeypatch.setattr(store, 'connection', original)

    old = store.get(state['run_id'])
    assert old['calculations'] == before['calculations']
    assert old['review_version'] == before['review_version'] == 1
    assert old['decision'] == before['decision']
    assert all(row['id'] != 'new-batch' for row in store.history())


def test_save_failure_rolls_back_latest_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    state = run(CLEAN, {})
    store.save(state['run_id'], state)
    pointer = store.latest()
    original = store.connection
    monkeypatch.setattr(store, 'connection', lambda: _FailingDB())
    with pytest.raises(DomainError):
        store.save('new-batch', dict(state, run_id='new-batch'))
    monkeypatch.setattr(store, 'connection', original)
    assert store.latest() == pointer
    assert store.get(pointer)['calculations'] == state['calculations']


def test_review_survives_failed_resave(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    state = run(CLEAN, {})
    store.save(state['run_id'], state)
    store.review(state['run_id'], 1, '通过')
    original = store.connection
    monkeypatch.setattr(store, 'connection', lambda: _FailingDB())
    with pytest.raises(DomainError):
        store.save('new-batch', dict(state, run_id='new-batch'))
    monkeypatch.setattr(store, 'connection', original)
    assert store.get(state['run_id'])['review_version'] == 2
    assert store.get(state['run_id'])['decision'] == '通过'


def test_same_observation_retry_keeps_single_batch(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    params = {'maintenance': window(), 'calibrations': [confirmed()]}
    first_state = run(CLEAN, params)
    first = store.save(first_state['run_id'], first_state)
    second_state = run(CLEAN, params)
    second = store.save(second_state['run_id'], second_state)
    assert first['run_id'] == second['run_id'] == store.latest()
    assert len(store.history()) == 1
    assert second['calculations'] == first['calculations']
    assert len(second_state['observations']) == len(first_state['observations'])


def test_export_includes_calibration_evidence():
    from app.modules.m7_archive.export_out import bundle
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed()]})
    text = bundle(state)
    assert '校准账本' in text and '观测采信' in text and '校准裁决' in text
    assert '校准裁决' in text


# --- API 与页面可见性 -------------------------------------------------------

def test_api_exposes_calibration_evidence(client):
    params = {'maintenance': window(regions=['A', 'B']), 'calibrations': [
        confirmed(offset=2, basis='维护后A新校准', regions=['A']),
        carried(offset=0, regions=['B'], basis='维护前B历史校准',
                bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00')]}
    data = client.post('/api/m3_versions', json={'sample': 'clean.csv', 'parameters': params}).get_json()
    rows = [r for r in data['observations'] if r['bucket'] == B1 and r['kind'] == 'zone']
    by_zone = {r['left']: r for r in rows}
    assert by_zone['A']['calibration']['basis'] == '维护后A新校准'
    assert by_zone['A']['calibration']['status'] == 'confirmed'
    assert by_zone['B']['calibration']['status'] == 'carried'
    assert by_zone['A']['calibration']['before'] == 120
    assert by_zone['A']['calibration']['after'] == 122
    full = client.post('/api/m5_calculate', json={'sample': 'clean.csv', 'parameters': params}).get_json()
    assert full['business']['certified_buckets'] and full['business']['carried_buckets']


def test_ingest_page_shows_basis_and_before_after(client):
    params = {'maintenance': window(regions=['A']),
              'calibrations': [confirmed(offset=2, regions=['A'], basis='维护后探头A校准依据')]}
    client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': params})
    page = client.get('/m3_versions')
    assert page.status_code == 200
    body = page.data.decode()
    assert '维护后探头A校准依据' in body
    assert '修正前' in body and '修正后' in body
    assert '未确认沿用' in body
    # 未补齐校准的时段在页面上明确标为不进入区间
    assert '不进入区间' in body or '整段隔离' in body


def test_save_failure_returns_503_and_keeps_old_batch(client, monkeypatch):
    first = client.post('/api/runs', json={'sample': 'clean.csv'}).get_json()
    original = store.save
    monkeypatch.setattr(store, 'save', lambda run_id, body: (_ for _ in ()).throw(
        DomainError('SAVE', '保存失败，已保留上一版本供核验', 503)))
    response = client.post('/api/runs', json={'sample': 'dirty.csv'})
    assert response.status_code == 503
    assert response.get_json()['code'] == 'SAVE'
    monkeypatch.setattr(store, 'save', original)
    assert client.get('/api/runs/' + first['run_id']).status_code == 200


def test_export_shows_before_after_for_formal_buckets():
    from app.modules.m7_archive.export_out import bundle
    state = run(CLEAN, {'maintenance': window(),
                        'calibrations': [confirmed(offset=2, regions=['A'])]})
    text = bundle(state)
    assert '观测采信' in text and '校准账本' in text
    assert 'WITHHELD' in text and 'CALIBRATION' in text


def test_export_quarantine_records_keep_basis_and_before_after():
    import csv, io
    from app.modules.m7_archive.export_out import bundle
    state = run(CLEAN, {'maintenance': window(), 'calibrations': [confirmed(regions=['A'])]})
    rows = list(csv.reader(io.StringIO(bundle(state).lstrip('﻿'))))
    withheld = [r for r in rows if r[0] == '隔离' and r[4] == 'CALIBRATION']
    assert withheld and all('"basis"' in r[5] and '"before"' in r[5] for r in withheld)


def test_pair_calibration_requires_explicit_kinds():
    params = {'maintenance': {'from': B1, 'to': '2026-11-16T01:59:59+00:00',
                              'regions': ['A'], 'kinds': ['pair']},
              'calibrations': [{'id': 'P', 'basis': '交集校准', 'status': 'confirmed',
                                'kinds': ['pair'], 'offset': 1}]}
    state = run(CLEAN, params)
    pairs = [o for o in state['observations'] if o['kind'] == 'pair' and o['bucket'] == B1]
    zones = [o for o in state['observations'] if o['kind'] == 'zone' and o['bucket'] == B1]
    assert all(o['calibration']['status'] == 'confirmed' and o['count'] == o['count_before'] + 1 for o in pairs)
    assert all(o['calibration']['status'] == 'raw' and o['count'] == o['count_before'] for o in zones)


def test_fractional_correction_withholds_bucket_deterministically():
    params = {'maintenance': {'from': B1, 'to': '2026-11-16T01:59:59+00:00', 'regions': ['A']},
              'calibrations': [confirmed(regions=['A'], factor='1.01',
                                         bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00')]}
    state = run(CLEAN, params)
    assert state['calibration']['withheld_buckets'] == [B1]
    assert state['calculations'][0]['code'] == 'WITHHELD'


def test_invalid_calibration_config_is_rejected():
    for bad in ([{'id': '', 'regions': ['A']}], [{'id': 'X', 'regions': ['D']}],
                [{'id': 'X', 'status': 'unknown'}], [{'id': 'X', 'factor': '0'}]):
        with pytest.raises(DomainError) as error:
            run(CLEAN, {'calibrations': bad})
        assert error.value.code == 'CALIBRATION'


def test_all_pages_render_with_withheld_buckets(client):
    params = {'maintenance': window(), 'calibrations': [confirmed(regions=['A'])]}
    client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': params})
    for mod in ('m1_ingest', 'm2_mapping', 'm3_versions', 'm4_segments', 'm5_calculate',
                'm6_reconcile', 'm7_archive'):
        response = client.get('/' + mod)
        assert response.status_code == 200, mod


def test_withheld_buckets_are_reused_when_nothing_changes():
    params = {'maintenance': window(), 'calibrations': [confirmed(regions=['A'])]}
    first = run(CLEAN, params)
    assert first['calibration']['withheld_buckets'] == [B1, B2]
    again = run(CLEAN, params, previous=first)
    assert again['recompute']['recomputed'] == []
    assert set(again['recompute']['reused']) == set(first['recompute']['recomputed'])
    assert again['business'] == first['business']
    assert len(again['quarantine']) == len(first['quarantine'])


def test_m1_page_shows_basis_and_before_after(client):
    params = {'maintenance': window(regions=['A', 'B']),
              'calibrations': [confirmed(offset=2, regions=['A'], basis='维护后探头A新校准'),
                               carried(offset=5, regions=['B'], basis='维护前B历史校准',
                                       bucket_from=B1, bucket_to='2026-11-16T01:59:59+00:00')]}
    client.post('/api/runs', json={'sample': 'clean.csv', 'parameters': params})
    body = client.get('/m1_ingest').data.decode()
    assert '每段校准依据与修正前后区间' in body
    assert '维护后探头A新校准' in body
    assert '修正前区间' in body and '修正后区间' in body
    assert '未确认沿用' in body and '已采信' in body
