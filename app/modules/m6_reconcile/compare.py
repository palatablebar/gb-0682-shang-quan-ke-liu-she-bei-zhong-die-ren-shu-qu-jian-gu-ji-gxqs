def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine'])
    accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    withheld=state.get('calibration_withheld') or []
    maintenance=(state.get('calibration') or {}).get('maintenance') or {}
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
        'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],
        'accounted':accounted,'balanced':accounted==state['input_count'],
        'business':state.get('business',{}),
        'calibration_withheld':len(withheld),
        'calibration_quarantine':sum(1 for q in state['quarantine'] if q.get('code') in ('CALIBRATION','CALIBRATION_CONFLICT')),
        'maintenance':maintenance,
        'recompute':state.get('recompute',{}),
    }
