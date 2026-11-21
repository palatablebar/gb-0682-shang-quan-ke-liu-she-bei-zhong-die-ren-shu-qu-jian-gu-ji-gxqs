from collections import defaultdict
from app.errors import DomainError


def model(observations):
    zones={r['left']:r['count'] for r in observations if r['kind']=='zone'}
    pairs={tuple(sorted([r['left'],r['right']])):r['count'] for r in observations if r['kind']=='pair'}
    if not 1<=len(zones)<=3 or not set(zones)<={'A','B','C'}:raise DomainError('CONSTRAINT','区域必须为A、B、C的子集')
    for (a,b),value in pairs.items():
        if a==b or a not in zones or b not in zones or value>min(zones[a],zones[b]):raise DomainError('CONSTRAINT','交集人数超出区域观测')
    if len(zones)==3 and len(pairs)!=3:raise DomainError('CONSTRAINT','三方估界需要三组两两交集')
    triple_rows=[r for r in observations if r['kind']=='triple']
    if triple_rows and (len(zones)!=3 or any(r['left']!='ABC' or r.get('right','')!='' for r in triple_rows)):
        raise DomainError('CONSTRAINT','三方交集必须对应ABC三个区域')
    triples=[r['count'] for r in triple_rows]
    if len(triples)>1:raise DomainError('CONSTRAINT','三方交集记录重复')
    return {'zones':zones,'pairs':{'|'.join(k):v for k,v in pairs.items()},'triple':triples[0] if triples else None}


def apply(state):
    grouped=defaultdict(list)
    for r in state['observations']:grouped[r['bucket']].append(r)
    prev=state.get('previous') or {}
    old=prev.get('bucket_signatures') or {}
    previous_models={item['bucket']:item for item in prev.get('models',[])}
    previous_rejected=set(prev.get('bucket_rejected') or [])
    signatures=state.get('bucket_signatures') or {}
    withheld=set((state.get('calibration') or {}).get('withheld_buckets') or [])
    result=[];bad_buckets=set();recomputed=[];reused=[]
    for bucket in sorted(set(grouped)|withheld):
        unchanged=bool(old) and old.get(bucket)==signatures.get(bucket)
        if unchanged and bucket in previous_models:
            item=dict(previous_models[bucket]);item['reused']=True;item['recalculated']=False
            result.append(item);reused.append(bucket);continue
        if unchanged and bucket in previous_rejected:
            bad_buckets.add(bucket);reused.append(bucket)
            for r in state['rows']:
                if r['bucket']==bucket:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':'CONSTRAINT','raw':r['_raw'],'reused':True})
            continue
        recomputed.append(bucket)
        if bucket in withheld:
            result.append({'bucket':bucket,'model':None,'line':min((r['_line'] for r in grouped[bucket]),default=0),
                'reused':False,'recalculated':True,'withheld':True});continue
        try:
            result.append({'bucket':bucket,'model':model(grouped[bucket]),
                'line':min(r['_line'] for r in grouped[bucket]),'reused':False,'recalculated':True,'withheld':False})
        except DomainError as e:
            bad_buckets.add(bucket)
            for r in state['rows']:
                if r['bucket']==bucket:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':e.code,'raw':r['_raw']})
    state['rows']=[r for r in state['rows'] if r['bucket'] not in bad_buckets];state['models']=result
    state['bucket_rejected']=sorted(bad_buckets)
    state['recompute']={'recomputed':sorted(recomputed),'reused':sorted(reused),
        'affected':(state.get('calibration') or {}).get('affected_buckets',[])}
    return state
