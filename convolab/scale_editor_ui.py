"""Scale-oriented presentation of the existing per-item questionnaire schema."""
from copy import deepcopy
import pandas as pd
import streamlit as st

from .config import Item, ConfigError, normalize_number
from .desktop import clean_grid, questionnaire_input


PHASE_LABELS={'pre':'Before the conversation','turn':'During the conversation','post':'After the conversation'}
TYPE_LABELS={'likert':'Rating (Likert scale)','text':'Open-ended response'}
SHARED_FIELDS=('type','min','max','min_label','max_label','pre','turn','post')


def scale_name(row):
    value=row.get('scale')
    return value.strip() if isinstance(value,str) else ''


def scale_groups(rows):
    groups={}
    for index,row in enumerate(rows):groups.setdefault(scale_name(row),[]).append(index)
    return list(groups.items())


def shared_settings(row):
    return {field:deepcopy(row.get(field,False if field in PHASE_LABELS else None)) for field in SHARED_FIELDS}


def field_guide():
    with st.expander('Question examples and field guide'):
        st.markdown('''**1. Set up one scale**

A scale is a group of questions with one shared answer type and survey timing.
For a rating scale, set the rating bounds and endpoint labels once for the whole scale.''')
        st.table(pd.DataFrame([
            {'Field':'Scale name','Example':'Trust','What to enter':'A name that groups these questions in your output.'},
            {'Field':'Answer type','Example':'Rating (Likert scale)','What to enter':'Choose a numbered rating or an open-ended written response.'},
            {'Field':'Lowest rating / label','Example':'1 / Strongly disagree','What to enter':'A whole number and words describing the low endpoint.'},
            {'Field':'Highest rating / label','Example':'7 / Strongly agree','What to enter':'A greater whole number and words describing the high endpoint.'},
            {'Field':'Survey timing','Example':'Before and after the conversation','What to enter':'Choose the times required by your study; this example does not select them for you.'},
        ]),hide_index=True)
        st.markdown('**2. Enter the questions in that scale**')
        st.table(pd.DataFrame([
            {'Question ID':'trust_1','Question wording':'I trust the other participants.'},
            {'Question ID':'trust_2','Question wording':'I can rely on the other participants.'},
        ]),hide_index=True)
        st.markdown('''Enter one question per row. Give each question its own complete wording and a unique ID using letters, numbers and underscores. IDs must be unique across the entire questionnaire; `reaction` is reserved.

**Open-ended scale:** use a name such as `Reflection`, choose **Open-ended response**, then enter questions such as `reflection_1` / “What influenced your decision?” Rating fields are not used. The default prompt asks for one to three sentences; you can inspect it under Advanced.

**Keep these points in mind**

- Questions in one scale share settings; different scales can use different settings.
- If no survey timing is selected, those questions are saved but are not asked.
- Imported or table-edited questions may have different settings within a scale. These stay unchanged until you explicitly apply shared settings.
- Responses are saved separately for each question. TeamLab does not calculate a combined scale score.''')


def settings_fields(values,key,disabled):
    result=deepcopy(values)
    previous_type=values.get('type')
    result['type']=st.selectbox('Answer type',list(TYPE_LABELS),
        index=list(TYPE_LABELS).index(previous_type) if previous_type in TYPE_LABELS else None,
        format_func=TYPE_LABELS.get,key=key+'type',disabled=disabled,
        placeholder='Choose an answer type',
        help='All questions in this scale use this answer type. Choosing Open-ended response clears rating-only settings for this scale.')
    st.write('When should agents answer the questions in this scale?')
    for column,(phase,label) in zip(st.columns(3),PHASE_LABELS.items()):
        with column:
            result[phase]=st.checkbox(label,value=bool(values.get(phase,False)),key=key+phase,disabled=disabled,
                help={'pre':'Ask every question in this scale before conversation messages begin.',
                      'turn':'Ask them at the interval in Run settings. Hidden passes do not trigger or postpone a scheduled survey.',
                      'post':'Ask them after a conversation ends normally. A failed conversation may have no final survey.'}[phase])
    if result['type']=='likert':
        for column,bound,label in zip(st.columns(2),('min','max'),('Lowest rating','Highest rating')):
            with column:
                raw=st.text_input(label,value='' if values.get(bound) is None else str(values[bound]),key=key+bound,
                    disabled=disabled,help='Enter a whole number shared by this scale. The highest rating must be greater than the lowest rating.') or None
                try:result[bound]=normalize_number(raw,bound,integer=True,optional=True)
                except ConfigError:result[bound]=raw
                result[bound+'_label']=st.text_input(label+' label',value=values.get(bound+'_label') or '',key=key+bound+'_label',
                    disabled=disabled,help='Describe this endpoint in words, such as Strongly disagree or Strongly agree. All questions in this scale use the same label.') or None
    elif result['type']=='text' and previous_type!='text':
        for field in ('min','max','min_label','max_label'):result[field]=None
    return result


def merge_question_rows(rows,indices,edited,settings,*,mixed,name,rename):
    """Keep original global order and per-item settings, with no UI columns saved."""
    retained={};added=[];allowed=set(indices)
    for row in edited:
        position=row.get('_source_index')
        if position is None:
            if mixed:raise ConfigError('Choose shared settings before adding questions to this scale.')
            added.append({'item_id':row.get('item_id'),'text':row.get('text'),'scale':name,**deepcopy(settings)})
            continue
        position=normalize_number(position,'question row',integer=True)
        if position not in allowed or position in retained:raise ConfigError('The question rows changed. Reload the editor before editing this scale.')
        item=deepcopy(rows[position]);item.update(item_id=row.get('item_id'),text=row.get('text'))
        if not mixed:item.update(deepcopy(settings))
        if rename:item['scale']=name
        retained[position]=item
    result=[]
    for position,row in enumerate(rows):
        if position not in allowed:result.append(deepcopy(row))
        elif position in retained:result.append(retained[position])
        if position==max(indices):result.extend(added)
    return result,bool(added or set(retained)!=allowed)


def scale_editor(rows,*,revision,prefix,state,key,disabled):
    st.caption('Select a scale or add a new one. Set its shared settings once, then enter one row per question. Your edits apply when you finish a field. Use Table only when you need to inspect or edit the individual saved item fields.')
    groups=scale_groups(rows);selected=None;updated=deepcopy(rows);redraw=False;desired_name=None
    if groups:
        selected=st.selectbox('Scale to edit',list(range(len(groups))),index=min(state['selected'],len(groups)-1),
            format_func=lambda n:f'Scale {n+1} — {groups[n][0] or "Unnamed scale"} ({len(groups[n][1])} '+('question)' if len(groups[n][1])==1 else 'questions)'),
            key=key+'scale_selected',disabled=disabled,help='Choose a scale to edit its shared settings and all of its questions. Switching scales keeps your edits.')
        state['selected']=selected
        group_name,indices=groups[selected];first=rows[indices[0]]
        entry=key+f'scale_{selected}_';initial=shared_settings(first)
        mixed=any(shared_settings(rows[index])!=initial for index in indices)
        with st.container(border=True):
            old_name=first.get('scale') or ''
            name=st.text_input('Scale name',value=old_name,key=entry+'name',disabled=disabled,
                placeholder='For example: Trust',help='Required grouping name for this scale. Questions with the same scale name are grouped together. Reusing another scale’s name combines their groups without changing existing question settings.')
            rename=name!=old_name;desired_name=name.strip() if rename else group_name
            apply_shared=None;settings=initial
            if mixed:
                st.warning('This scale has different settings across its questions. Its existing settings are preserved. To make them the same, choose shared settings below and click Apply shared settings to this scale. Until then, any execution uses the original item settings.')
                with st.expander('View current question settings'):
                    st.dataframe(pd.DataFrame([rows[i] for i in indices]),hide_index=True)
                choose=st.checkbox('Set shared settings for this scale',key=entry+'choose_shared',disabled=disabled,
                    help='Open a blank shared-settings draft. Nothing is applied until you click Apply shared settings to this scale.')
                if choose:
                    draft=settings_fields({field:False if field in PHASE_LABELS else None for field in SHARED_FIELDS},entry+'draft_',disabled)
                    if st.button('Apply shared settings to this scale',key=entry+'apply',disabled=disabled):
                        try:
                            validated=Item.model_validate({'item_id':'draft','scale':'Draft','text':'Draft',**draft})
                            apply_shared=shared_settings(validated.model_dump())
                        except ValueError:st.error('Please choose an answer type. Rating scales also need whole-number bounds with lowest < highest and a label for each endpoint.')
            else:
                st.markdown('**Shared settings for this scale**')
                settings=settings_fields(initial,entry+'shared_',disabled)
                if not any(settings.get(phase) for phase in PHASE_LABELS):st.info('No timing selected: the questions in this scale will be saved but will not be asked.')
            st.markdown('**Questions in this scale**')
            st.caption('Enter a unique Question ID and the complete Question wording on each row. For example: trust_1 — “I trust the other participants.” Add rows at the bottom; use the row menu to remove a question. All new rows inherit this scale’s shared settings.' if not mixed else 'You can edit existing IDs and wording here. Choose shared settings before adding or removing rows, or use Table to edit individual items.')
            pair_rows=[{'_source_index':i,'item_id':rows[i].get('item_id'),'text':rows[i].get('text')} for i in indices]
            baseline=questionnaire_input(st.session_state,prefix+'scale_questions_input',(revision,state['generation'],selected),pair_rows)
            frame=pd.DataFrame(baseline,columns=['_source_index','item_id','text'])
            grid=st.data_editor(frame,num_rows='fixed' if mixed else 'dynamic',hide_index=True,key=entry+'scale_questions_grid',
                disabled=True if disabled else ['_source_index'],column_order=['item_id','text'],column_config={
                    '_source_index':None,'item_id':st.column_config.TextColumn('Question ID',width='small',help='Required unique ID across all scales. Use letters, numbers and underscores; reaction is reserved.'),
                    'text':st.column_config.TextColumn('Question wording',width='large',help='Enter the full question or statement. The scale’s shared settings apply to this question.')})
            try:
                updated,structural=merge_question_rows(rows,indices,clean_grid(grid.to_dict('records')),settings,mixed=mixed,name=name,rename=rename)
                redraw=structural or rename
                if apply_shared is not None:
                    for item in updated:
                        if scale_name(item)==desired_name:item.update(deepcopy(apply_shared))
                    redraw=True
            except ConfigError as error:st.error(str(error));updated=deepcopy(rows)
        with st.expander('Questionnaire overview'):
            st.dataframe(pd.DataFrame([{'Scale':scale_name(row),'Question ID':row.get('item_id'),'Question wording':row.get('text'),
                'Answer type':TYPE_LABELS.get(row.get('type'),row.get('type')),
                'Timing':', '.join(label for phase,label in PHASE_LABELS.items() if row.get(phase)) or 'Not asked'} for row in updated]),hide_index=True)
    else:st.info('No scales yet. Add a scale, upload a questionnaire CSV, or continue without questionnaire measures.')
    add_column,remove_column=st.columns(2)
    has_unnamed=any(not name for name,_ in scale_groups(updated))
    with add_column:
        add=st.button('Add a scale',key=key+'add_scale',disabled=disabled or has_unnamed,
            help='Add a blank scale with one empty question. Name the scale before adding another one. No question wording, rating bounds or survey timing is chosen for you.')
    with remove_column:
        remove=st.button('Remove this scale and its questions',key=key+'remove_scale',disabled=disabled or selected is None,
            help='Remove this entire scale from the current setup. Previously saved study files and results stay unchanged.')
    if not disabled and remove:
        updated=[deepcopy(row) for i,row in enumerate(rows) if i not in set(groups[selected][1])]
        desired_name=None;redraw=True
    elif not disabled and add:
        updated.append({'item_id':None,'scale':None,'text':None,'type':'likert',
            'min':None,'max':None,'min_label':None,'max_label':None,'pre':False,'turn':False,'post':False})
        desired_name='';redraw=True
    if redraw and not disabled:
        new_groups=scale_groups(updated);names=[name for name,_ in new_groups]
        state['selected']=names.index(desired_name) if desired_name in names else min(state['selected'],max(0,len(new_groups)-1))
        state['generation']+=1
    return updated,redraw and not disabled
