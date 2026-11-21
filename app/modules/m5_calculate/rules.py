from app.errors import DomainError
from app.modules.m4_segments.rules import model as build_model


def bounds(model):
    zones=model['zones'];keys=sorted(zones);pairs=model['pairs'];values=[zones[k] for k in keys]
    if len(keys)==1:return values[0],values[0],0,0
    if len(keys)==2:
        common=pairs.get('|'.join(keys))
        return (sum(values)-common,sum(values)-common,common,common) if common is not None else (max(values),sum(values),0,min(values))
    a,b,c=keys;ab=pairs['|'.join([a,b])];ac=pairs['|'.join([a,c])];bc=pairs['|'.join([b,c])]
    low=max(0,ab+ac-zones[a],ab+bc-zones[b],ac+bc-zones[c]);high=min(ab,ac,bc)
    if model.get('triple') is not None:low=max(low,model['triple']);high=min(high,model['triple'])
    if low>high:raise DomainError('CONSTRAINT','三方观测不存在可行人数')
    base=sum(values)-ab-ac-bc
    return base+low,base+high,low,high


def _raw_model(state,bucket):
    """修正前的模型：只把人数换回 count_before，其余口径不变。"""
    rows=[]
    for r in state['observations']:
        if r['bucket']==bucket:
            item=dict(r);item['count']=r.get('count_before',r['count']);rows.append(item)
    return build_model(rows)


def _withheld_entry(state,bucket):
    items=[r for r in (state.get('calibration_withheld') or []) if r['bucket']==bucket]
    raw=[]
    for r in items:
        item=dict(r);item['count']=r.get('count_before',r['count']);raw.append(item)
    entry={'id':bucket,'line':min((r['_line'] for r in items),default=0),'lower':None,'upper':None,
        'width':None,'triple_lower':None,'triple_upper':None,'code':'WITHHELD',
        'reused':False,'recalculated':True,'before':None,'delta':None,
        'reason':'维护期校准未补齐，整段不进入正式区间'}
    try:
        low,high,_,_=bounds(build_model(raw))
        entry['before']={'lower':low,'upper':high,'code':'EXACT' if low==high else 'INTERVAL'}
    except DomainError:
        entry['before']=None
    return entry


def apply(state):
    previous=(state.get('previous') or {}).get('calculations',[])
    previous_by_id={item['id']:item for item in previous}
    previous_rejected=set((state.get('previous') or {}).get('calculation_rejected') or [])
    withheld_buckets=set((state.get('calibration') or {}).get('withheld_buckets') or [])
    results=[];bad=set();recomputed=[];reused=[]
    for item in state['models']:
        bucket=item['bucket']
        if bucket in withheld_buckets:
            entry=_withheld_entry(state,bucket);entry['reused']=bool(item.get('reused'))
            entry['recalculated']=not item.get('reused');results.append(entry)
            (reused if item.get('reused') else recomputed).append(bucket)
            continue
        if item.get('reused') and bucket in previous_by_id:
            entry=dict(previous_by_id[bucket]);entry['reused']=True;entry['recalculated']=False
            results.append(entry);reused.append(bucket);continue
        if item.get('reused') and bucket in previous_rejected:
            bad.add(bucket);reused.append(bucket)
            for r in state['rows']:
                if r['bucket']==bucket:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':'CONSTRAINT','raw':r['_raw'],'reused':True})
            continue
        recomputed.append(bucket)
        try:
            low,high,tlo,thi=bounds(item['model'])
        except DomainError as e:
            bad.add(bucket)
            for r in state['rows']:
                if r['bucket']==bucket:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':e.code,'raw':r['_raw']})
            continue
        entry={'id':bucket,'line':item['line'],'lower':low,'upper':high,'width':high-low,
            'triple_lower':tlo,'triple_upper':thi,'code':'EXACT' if low==high else 'INTERVAL',
            'reused':False,'recalculated':True,'before':None,'delta':None,'reason':''}
        try:
            blow,bhigh,_,_=bounds(_raw_model(state,bucket))
            entry['before']={'lower':blow,'upper':bhigh,'code':'EXACT' if blow==bhigh else 'INTERVAL'}
            entry['delta']={'lower':low-blow,'upper':high-bhigh}
        except DomainError:
            entry['before']=None;entry['delta']=None
        results.append(entry)
    state['rows']=[r for r in state['rows'] if r['bucket'] not in bad]
    state['calculations']=results
    state['calculation_rejected']=sorted(bad)
    state['recompute']={'recomputed':sorted(set(recomputed)),'reused':sorted(set(reused)),
        'affected':(state.get('calibration') or {}).get('affected_buckets',[]),
        'withheld':sorted(withheld_buckets)}
    formal=[r for r in results if r.get('lower') is not None]
    certified=[r for r in formal if all(
        (o.get('calibration') or {}).get('status')!='carried'
        for o in state['observations'] if o['bucket']==r['id'])]
    carried=[r for r in formal if r not in certified]
    state['business']={
        'total':sum(r['lower'] for r in formal),'upper_total':sum(r['upper'] for r in formal),
        'buckets':len(formal),'unit':'客次',
        'certified_total':sum(r['lower'] for r in certified),'certified_upper_total':sum(r['upper'] for r in certified),
        'certified_buckets':[r['id'] for r in certified],
        'carried_lower':sum(r['lower'] for r in carried),'carried_upper':sum(r['upper'] for r in carried),
        'carried_buckets':[r['id'] for r in carried],
        'carried_observations':sum(1 for r in state['observations'] if (r.get('calibration') or {}).get('status')=='carried'),
        'withheld_buckets':sorted(withheld_buckets),
    }
    return state
