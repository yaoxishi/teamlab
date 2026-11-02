"""Questionnaire presentation only; original validation and export fields stay intact."""
from copy import deepcopy
import pandas as pd
import streamlit as st

from .config import ITEM_COLUMNS
from .desktop import clean_grid, questionnaire_input


from .scale_editor_ui import PHASE_LABELS, TYPE_LABELS, field_guide, scale_editor


def questionnaire_editor(rows, *, revision, prefix, baseline_key, disabled=False):
    """Return current edits plus a redraw flag; caller commits before redrawing.

    Only one input layout is active. Changing layout or adding/removing a row
    starts fresh widget identities using already-committed rows, never replaying
    a previous table's cumulative deltas. Hosted state uses its private prefix.
    """
    rows = [{field: deepcopy(row.get(field, False if field in PHASE_LABELS else None))
             for field in ITEM_COLUMNS} for row in rows]
    state = st.session_state.setdefault(prefix + 'questionnaire_ui', {'layout': None, 'generation': 0, 'selected': 0})
    if st.session_state.get(prefix+'questionnaire_layout') not in (None,'One scale at a time','Table'):
        st.session_state.pop(prefix+'questionnaire_layout',None)
    layout = st.radio('Question editor', ['One scale at a time', 'Table'], horizontal=True,
        key=prefix + 'questionnaire_layout', disabled=disabled,
        help='Use One scale at a time to enter several questions with shared settings. Table exposes individual item fields, including different settings within an imported scale. Both layouts edit the same questionnaire.')
    if state['layout'] != layout:
        state.update(layout=layout, generation=state['generation'] + 1)
    key = prefix + f'questions_{state["generation"]}_'
    st.caption('**Note:** Questions within one scale share the answer type and survey timing, plus the rating bounds and labels for rating scales. Different scales can use different settings. Leave the questionnaire empty if you do not need these measures.')
    st.caption('**Note:** Open-ended responses are supported. Collecting written answers after every conversation turn can use more tokens and increase API costs. You can adjust the survey interval and the questionnaire response limit in Run settings.')
    field_guide()
    if layout == 'Table':
        frame = pd.DataFrame(questionnaire_input(st.session_state, baseline_key,
            (revision, state['generation']), rows), columns=ITEM_COLUMNS)
        for phase in PHASE_LABELS:
            frame[phase] = frame[phase].astype('boolean')
        config = {
            'item_id': st.column_config.TextColumn('Question ID', help='Required unique ID: letters, numbers and underscores. Do not use reaction.'),
            'scale': st.column_config.TextColumn('Measure / scale name', help='Required group name for this question, such as Trust or Reflection.'),
            'text': st.column_config.TextColumn('Question wording', width='large', help='Enter the complete question each agent should answer.'),
            'type': st.column_config.SelectboxColumn('Answer type', options=['likert', 'text'], help='likert means a numbered rating; text means an open-ended written answer.'),
            'min': st.column_config.NumberColumn('Lowest rating', step=1, help='Whole number for rating questions. Leave empty for text questions.'),
            'max': st.column_config.NumberColumn('Highest rating', step=1, help='Whole number greater than the lowest rating. Leave empty for text questions.'),
            'min_label': st.column_config.TextColumn('Lowest rating label', help='Describe the low endpoint, such as Not at all. Empty for text questions.'),
            'max_label': st.column_config.TextColumn('Highest rating label', help='Describe the high endpoint, such as Completely. Empty for text questions.'),
            **{phase: st.column_config.CheckboxColumn(label, default=False,
                help='Ask this question ' + {'pre': 'before the conversation starts.', 'turn': 'at the during-conversation survey interval.', 'post': 'after the conversation ends normally.'}[phase])
                for phase, label in PHASE_LABELS.items()}}
        for bound in ('min', 'max'):
            if any(isinstance(value, str) for value in frame[bound]):
                config[bound] = st.column_config.TextColumn('Lowest rating' if bound=='min' else 'Highest rating',
                    help='Enter a whole number for a rating question. Leave empty for an open-ended question.')
        grid = st.data_editor(frame, num_rows='dynamic', hide_index=True, key=key + 'items_grid',
            disabled=disabled, column_config=config,
            column_order=['item_id', 'text', 'type', 'pre', 'turn', 'post', 'scale', 'min', 'min_label', 'max', 'max_label'])
        return clean_grid(grid.to_dict('records')), False

    st.session_state.pop(baseline_key, None)
    return scale_editor(rows, revision=revision, prefix=prefix, state=state, key=key, disabled=disabled)
