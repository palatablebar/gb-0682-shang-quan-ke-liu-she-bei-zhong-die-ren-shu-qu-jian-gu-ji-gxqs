"""维护期校准裁决。

探头维护后，观测可能仍停留在维护前的历史校准口径。这里把“哪条观测可以
进入正式区间”固化成可复核的规则，而不是在页面或导出时临时判断：

* ``confirmed``：已完成的新校准，可进入正式区间，并计入“已采信”。
* ``carried``：沿用历史校准且尚未确认，可进入正式区间，但只能以
  “未确认沿用”出现，不得计入“已采信”。
* 无可用校准、或同一条观测命中互相矛盾的多条校准：隔离，保留原值。

修正一律以观测的原始人数为基数一次性替换计算（``raw * factor + offset``），
不以已经修正过的人数为基数，因此重试不会叠加修正。
"""
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.errors import DomainError
from app.modules.m2_mapping.coerce import instant

REGION_ALIASES = {'东门': 'A', '中庭': 'B', '西门': 'C', 'A': 'A', 'B': 'B', 'C': 'C'}
TERMINAL = {'A', 'B', 'C'}
MAX_COUNT = Decimal(1000000)

STATUS_ALIASES = {
    'confirmed': 'confirmed', 'new': 'confirmed', '已确认': 'confirmed', '已采信': 'confirmed',
    'carried': 'carried', 'historical': 'carried', 'history': 'carried', 'provisional': 'carried',
    'stale': 'carried', '沿用': 'carried', '未确认': 'carried', '历史': 'carried',
}
HISTORICAL_SOURCES = {'historical', 'history', 'carry', 'carried', 'legacy', '历史', '沿用'}

POLICY = [
    '维护窗口内已完成新校准的观测进入正式区间，并计入已采信人数。',
    '维护窗口内未完成新校准、但可沿用历史校准的观测，以“未确认沿用”进入正式区间；'
    '在交接与归档中单独列示，不计入已采信人数。',
    '维护窗口内没有任何可用校准依据的观测隔离，原因码 CALIBRATION。',
    '同一观测命中多条互相矛盾的校准（含同编号不同内容）时隔离，原因码 CALIBRATION_CONFLICT，'
    '冲突校准记录保留在校准账本与审计中。',
    '修正以原始人数为基数一次性替换；同一观测重试或重复提交同一校准不会叠加修正。',
]


def _dec(value, field, default=None):
    if value is None:
        if default is None:
            raise DomainError('CALIBRATION', f'校准缺少 {field}')
        return Decimal(default)
    try:
        result = Decimal(str(value).replace(',', '').strip())
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise DomainError('CALIBRATION', f'校准 {field} 无效')


def _text_list(value, field):
    if value is None:
        return None
    if isinstance(value, str):
        parts = value.replace('，', ',').replace(';', ',').replace('|', ',').split(',')
    elif isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        raise DomainError('CALIBRATION', f'校准 {field} 格式无效')
    return [p.strip() for p in parts if p and p.strip()]


def _regions(value):
    parts = _text_list(value, 'regions')
    if parts is None:
        return None
    regions = set()
    for part in parts:
        alias = REGION_ALIASES.get(part)
        if alias is None:
            raise DomainError('CALIBRATION', '校准区域必须为A、B、C')
        regions.add(alias)
    return regions


def _kinds(value):
    parts = _text_list(value, 'kinds')
    if parts is None:
        return None
    kinds = set(parts)
    if not kinds <= {'zone', 'pair', 'triple'}:
        raise DomainError('CALIBRATION', '校准类型必须为zone、pair、triple')
    return kinds


def _ids(value):
    parts = _text_list(value, 'ids')
    return set(parts) if parts is not None else None


def _time(value, field):
    if value in (None, ''):
        return None
    try:
        return datetime.fromisoformat(instant(value))
    except (ValueError, AttributeError):
        raise DomainError('CALIBRATION', f'校准 {field} 必须包含时区')


def _iso(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


def _normalize(item, index):
    if not isinstance(item, dict):
        raise DomainError('CALIBRATION', '校准记录必须是对象')
    rid = str(item.get('id', item.get('calibration_id', ''))).strip()
    if not rid:
        raise DomainError('CALIBRATION', '校准记录缺少编号')
    basis = str(item.get('basis', item.get('calibration_basis', ''))).strip() or f'校准 {rid}'
    raw_status = str(item.get('status', item.get('state', ''))).strip().lower()
    confirmed = item.get('confirmed')
    if not raw_status:
        source = str(item.get('source', '')).strip().lower()
        raw_status = 'carried' if (source in HISTORICAL_SOURCES or confirmed is False) else 'confirmed'
    status = STATUS_ALIASES.get(raw_status)
    if status is None:
        raise DomainError('CALIBRATION', f'校准 {rid} 状态无效')
    source = str(item.get('source', '')).strip().lower()
    if source in HISTORICAL_SOURCES:
        status = 'carried'

    factor = _dec(item.get('factor', item.get('scale')), 'factor', '1')
    if factor <= 0:
        raise DomainError('CALIBRATION', f'校准 {rid} 修正系数必须大于0')
    offset = _dec(item.get('offset', item.get('delta', item.get('count_delta'))), 'offset', '0')

    regions = _regions(item.get('regions', item.get('region')))
    kinds = _kinds(item.get('kinds', item.get('kind')))
    ids = _ids(item.get('ids', item.get('observation_ids')))
    if kinds is None:
        # 未声明 kinds 时，区域校准只修该探头自身的区域人数。交集/三方人数来自
        # 重叠检测，不能被单探头口径替代；需要修交集时必须显式声明 kinds。
        kinds = {'zone'} if regions else None

    record = {
        'index': index,
        'id': rid,
        'basis': basis,
        'status': status,
        'source': source or ('historical' if status == 'carried' else 'new'),
        'regions': sorted(regions) if regions else [],
        'kinds': sorted(kinds) if kinds else None,
        'ids': sorted(ids) if ids else None,
        'bucket_from': _iso(_time(item.get('bucket_from', item.get('from')), 'bucket_from')),
        'bucket_to': _iso(_time(item.get('bucket_to', item.get('to')), 'bucket_to')),
        'factor': str(factor),
        'offset': str(offset),
        'confirmed_at': item.get('confirmed_at'),
        '_factor': factor,
        '_offset': offset,
        '_from': _time(item.get('bucket_from', item.get('from')), 'bucket_from'),
        '_to': _time(item.get('bucket_to', item.get('to')), 'bucket_to'),
        '_conflicted': False,
    }
    if record['_from'] and record['_to'] and record['_from'] > record['_to']:
        raise DomainError('CALIBRATION', f'校准 {rid} 时间窗起始晚于结束')
    return record


def _canonical(record):
    return json.dumps({k: v for k, v in record.items() if not k.startswith('_') and k != 'index'},
                      sort_keys=True, ensure_ascii=False)


class Ledger:
    def __init__(self, records, conflicted, audit):
        self.records = records
        self.conflicted = conflicted
        self.audit = audit

    def public(self):
        return [{k: v for k, v in record.items() if not k.startswith('_')} for record in self.records]


def ledger(parameters):
    raw = parameters.get('calibrations', parameters.get('calibration', []))
    if raw in (None, '') or raw == []:
        return Ledger([], [], [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise DomainError('CALIBRATION', '校准配置必须是数组或JSON')
    if not isinstance(raw, list):
        raise DomainError('CALIBRATION', '校准配置必须是数组')
    records, seen, conflicted, audit = [], {}, set(), []
    for index, item in enumerate(raw):
        record = _normalize(item, index)
        canon = _canonical(record)
        rid = record['id']
        if rid in seen:
            previous = seen[rid]
            if _canonical(previous) == canon:
                audit.append({'event': 'duplicate_ignored', 'calibration_id': rid, 'index': index,
                              'message': '重复校准记录已去重，只应用一次'})
                continue
            previous['_conflicted'] = True
            record['_conflicted'] = True
            conflicted.add(rid)
            records.append(record)
            audit.append({'event': 'conflict', 'calibration_id': rid, 'index': index,
                          'message': '同一校准编号出现不同内容，相关观测将隔离'})
            continue
        seen[rid] = record
        records.append(record)
    return Ledger(records, conflicted, audit)


def maintenance(parameters):
    raw = parameters.get('maintenance')
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise DomainError('CALIBRATION', '维护窗口必须是对象')
    if raw.get('active') is False:
        return None
    regions = _regions(raw.get('regions', raw.get('region')))
    # 维护窗口默认针对区域人数；重叠观测若也要纳入，须显式声明 kinds。
    kinds = _kinds(raw.get('kinds', raw.get('kind'))) or {'zone'}
    start = _time(raw.get('from', raw.get('start')), 'maintenance.from')
    end = _time(raw.get('to', raw.get('end')), 'maintenance.to')
    if start and end and start > end:
        raise DomainError('CALIBRATION', '维护窗口起始晚于结束')
    return {
        'regions': sorted(regions) if regions else None,
        'kinds': sorted(kinds),
        'from': _iso(start),
        'to': _iso(end),
        'label': str(raw.get('label', raw.get('reason', '探头维护'))),
        '_from': start,
        '_to': end,
    }


def public_maintenance(config):
    if not config:
        return None
    return {k: v for k, v in config.items() if not k.startswith('_')}


def _names(observation):
    if observation.get('kind') == 'triple':
        return set(TERMINAL)
    return {observation.get('left'), observation.get('right')} - {None, ''}


def _in_scope(record, observation):
    if record['ids'] is not None:
        return str(observation.get('id', '')) in record['ids']
    if record['kinds'] is not None and observation.get('kind') not in record['kinds']:
        return False
    if record['regions'] and not (set(record['regions']) & _names(observation)):
        return False
    bucket = datetime.fromisoformat(observation['bucket'])
    if record['_from'] and bucket < record['_from']:
        return False
    if record['_to'] and bucket > record['_to']:
        return False
    return True


def affected(config, observation):
    if not config:
        return False
    if observation.get('kind') not in config['kinds']:
        return False
    if config['regions'] and not (set(config['regions']) & _names(observation)):
        return False
    bucket = datetime.fromisoformat(observation['bucket'])
    if config['_from'] and bucket < config['_from']:
        return False
    if config['_to'] and bucket > config['_to']:
        return False
    return True


def decide(book, window, observation):
    """裁决一条观测。返回动作、采用的校准与全部命中记录。

    已确认的新校准优先于历史沿用：维护后补齐校准即视为取代旧口径。只有
    同一优先级内出现不同修正、或命中同编号不同内容的校准，才算冲突隔离。
    """
    matches = [record for record in book.records if _in_scope(record, observation)]
    hit_ids = [record['id'] for record in matches]
    if any(record['_conflicted'] for record in matches):
        return {'action': 'quarantine', 'code': 'CALIBRATION_CONFLICT', 'matches': hit_ids,
                'basis': '校准编号冲突', 'status': 'conflict'}
    confirmed = [record for record in matches if record['status'] == 'confirmed']
    pool = confirmed or matches
    distinct = {(record['_factor'], record['_offset']) for record in pool}
    if len(distinct) > 1:
        return {'action': 'quarantine', 'code': 'CALIBRATION_CONFLICT', 'matches': hit_ids,
                'basis': '多条校准修正互相矛盾', 'status': 'conflict'}
    if matches:
        chosen = sorted(pool, key=lambda record: (record['status'] != 'confirmed', record['id']))[0]
        return {'action': 'apply', 'calibration': chosen, 'matches': hit_ids,
                'status': chosen['status'], 'basis': chosen['basis'], 'code': None}
    if affected(window, observation):
        return {'action': 'quarantine', 'code': 'CALIBRATION', 'matches': [],
                'basis': '维护窗口内没有可用校准依据', 'status': 'missing'}
    return {'action': 'raw', 'matches': [], 'basis': '未配置校准', 'status': 'raw', 'code': None}


def corrected_count(raw, record):
    try:
        value = Decimal(str(raw)) * record['_factor'] + record['_offset']
    except (InvalidOperation, ValueError):
        raise DomainError('CALIBRATION', '修正人数无效')
    if not value.is_finite() or value < 0 or value != value.to_integral_value() or value > MAX_COUNT:
        raise DomainError('CALIBRATION', '修正人数必须为非负整数')
    return int(value)
