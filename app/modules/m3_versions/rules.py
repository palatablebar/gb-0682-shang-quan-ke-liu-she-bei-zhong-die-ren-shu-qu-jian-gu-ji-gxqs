from collections import defaultdict
from decimal import Decimal
from datetime import datetime
import json

from app.errors import DomainError
from app.modules.m3_versions.calibration import (
    POLICY, corrected_count, decide, ledger, maintenance, public_maintenance,
)

ALIASES={'东门':'A','中庭':'B','西门':'C','A':'A','B':'B','C':'C'}
CALIBRATION_CODES={'CALIBRATION','CALIBRATION_CONFLICT'}


def _evidence(decision, before, after=None):
    chosen=decision.get('calibration') or {}
    return {
        'basis':decision.get('basis',''),
        'status':decision.get('status',''),
        'action':decision.get('action',''),
        'code':decision.get('code'),
        'calibration_ids':decision.get('matches',[]),
        'source':chosen.get('source',''),
        'before':before,
        'after':after if after is not None else before,
        'delta':(after-before) if after is not None else 0,
    }


def _signature(rows):
    return json.dumps(sorted(
        (str(r['id']),r['kind'],str(r['left']),str(r['right']),int(r['_base_count']),
         r.get('count'),(r.get('calibration') or {}).get('status'),
         (r.get('calibration') or {}).get('code'),r.get('calibration_applied'))
        for r in rows),ensure_ascii=False)


def _restore(row):
    """撤销上一次本模块的修正，回到单位换算后的原始人数。

    修正永远以原始人数为基数重算，因此同一观测重复提交或重复调用不会叠加
    修正；上一轮的校准隔离会被重新裁决，不会重复产生隔离记录。
    """
    if '_base_count' in row:row['count']=row['_base_count']
    for key in ('calibration_applied','calibration','count_before'):row.pop(key,None)


def _prepare(rows):
    groups=defaultdict(list);bad=[]
    for r in rows:
        r['bucket']=datetime.fromisoformat(r['bucket']).replace(minute=0,second=0,microsecond=0).isoformat()
        r['left']=ALIASES.get(r['left'],r['left']);r['right']=ALIASES.get(r['right'],r['right'])
        if '_base_count' in r:
            value=Decimal(str(r['_base_count']))
        else:
            value=Decimal(str(r['count']))
            factor={'人':Decimal(1),'千人':Decimal(1000)}.get(r['unit'])
            if factor is None:bad.append((r,'UNIT'));continue
            value*=factor
        if value<0 or value!=value.to_integral_value() or value>1000000 or r['kind'] not in ('zone','pair','triple'):
            bad.append((r,'RANGE'));continue
        r['_base_count']=int(value);r['count']=int(value)
        if r['kind']=='pair':r['left'],r['right']=sorted([r['left'],r['right']])
        groups[(r['bucket'],r['kind'],r['left'],r['right'])].append(r)
    return groups,bad


def _select(groups):
    selected=[];bad=[]
    for rows in groups.values():
        high=max(Decimal(str(r['version'])) for r in rows)
        latest=[r for r in rows if Decimal(str(r['version']))==high]
        if len({r['count'] for r in latest})>1:bad.extend((r,'CONFLICT') for r in latest)
        else:selected.append(sorted(latest,key=lambda r:r['id'])[0])
    return selected,bad


def _withhold(items,code,basis,status):
    """整桶不进入正式区间。

    只用同桶剩余观测拼联合人数会系统性漏计被隔离的区域，因此宁可整段交出
    空档，也不把残缺区域口径冒充当天客流。原值、校准依据和来源行全部保留。
    """
    decision={'action':'quarantine','code':code,'basis':basis,'status':status,'matches':[]}
    for r in items:
        r['count']=r['_base_count']
        r['calibration']=_evidence(decision,r['_base_count']);r['calibration_applied']=False
    return _signature(items)


def _decide_bucket(items,book,window):
    decisions=[]
    for r in items:
        try:decisions.append((r,decide(book,window,r)))
        except DomainError as error:
            decisions.append((r,{'action':'quarantine','code':error.code,'matches':[],
                'basis':error.message,'status':'invalid'}))
    # 整段使用同一个原因码；冲突比“缺校准”更需要优先暴露给复核人。
    failed=[d for _,d in decisions if d['action']=='quarantine']
    if failed:
        chosen=next((d for d in failed if d['code']=='CALIBRATION_CONFLICT'),failed[0])
        return None,_withhold(items,chosen['code'],chosen['basis'],chosen['status'])
    for r,d in decisions:
        if d['action']=='apply':
            try:corrected_count(r['count'],d['calibration'])
            except DomainError as error:
                return None,_withhold(items,'CALIBRATION',error.message,'invalid')
    kept=[];counts=defaultdict(int);changed=False
    for r,decision in decisions:
        before=r['count']
        if decision['action']=='apply':
            after=corrected_count(before,decision['calibration'])
            r['count_before']=before;r['count']=after
            r['calibration']=_evidence(decision,before,after);r['calibration_applied']=True
            counts[decision['status']]+=1;changed=changed or before!=after
        else:
            r['count_before']=before;r['calibration']=_evidence(decision,before)
            r['calibration_applied']=False;counts['raw']+=1
        kept.append(r)
    return (kept,counts,changed),_signature(kept)


def apply(state):
    book=ledger(state.get('parameters',{}))
    window=maintenance(state.get('parameters',{}))
    # 重试：把上一轮校准隔离的观测放回待裁决集合，并清除上一轮校准隔离记录。
    rows=list(state['rows'])+list(state.pop('calibration_withheld',[]))
    state['quarantine']=[q for q in state['quarantine'] if q.get('code') not in CALIBRATION_CODES]
    for r in rows:_restore(r)

    groups,bad=_prepare(rows)
    selected,version_bad=_select(groups)
    bad+=version_bad
    by_bucket=defaultdict(list)
    for r in selected:by_bucket[r['bucket']].append(r)

    quarantine=[];affected=set();withheld=[];kept=[];signatures={}
    counts={'confirmed':0,'carried':0,'raw':0,'calibration_quarantine':0}
    for bucket in sorted(by_bucket):
        items=by_bucket[bucket]
        outcome,signature=_decide_bucket(items,book,window)
        signatures[bucket]=signature
        if outcome is None:
            counts['calibration_quarantine']+=len(items);affected.add(bucket);withheld.extend(items)
            for r in items:
                evidence=r['calibration']
                quarantine.append({'line':r['_line'],'id':r['id'],'code':evidence['code'],
                    'raw':r['_raw'],'bucket':bucket,'calibration':evidence,'bucket_withheld':True})
            continue
        bucket_kept,bucket_counts,changed=outcome
        for status,count in bucket_counts.items():counts[status]+=count
        if changed:affected.add(bucket)
        kept.extend(bucket_kept)

    badlines={r['_line'] for r,_ in bad}|{r['_line'] for r in withheld}
    state['rows']=[r for r in state['rows'] if r['_line'] not in badlines]
    for r,c in bad:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':c,'raw':r['_raw']})
    state['quarantine'].extend(quarantine)
    state['calibration_withheld']=withheld
    state['observations']=kept
    state['bucket_signatures']=signatures
    state['calibration']={
        'rules':POLICY,
        'maintenance':public_maintenance(window),
        'records':book.public(),
        'audit':book.audit,
        'affected_buckets':sorted(affected),
        'withheld_buckets':sorted({r['bucket'] for r in withheld}),
        'confirmed_observations':counts['confirmed'],
        'carried_observations':counts['carried'],
        'uncorrected_observations':counts['raw'],
        'calibration_quarantine':counts['calibration_quarantine'],
        'conflict_calibrations':sorted(book.conflicted),
    }
    return state
