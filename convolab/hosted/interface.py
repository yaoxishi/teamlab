"""Prototype UI using original validators, prompt builders and research readers."""
import os
from pathlib import Path
from uuid import uuid4
import altair as alt
import pandas as pd
import streamlit as st

from convolab.config import BIG_FIVE_FIELDS, DEMOGRAPHIC_FIELDS, ITEM_COLUMNS, PERSONA_COLUMNS, SURVEY_INSTRUCTIONS, ConfigError
from convolab.desktop import EXAMPLES,builtin_bytes,builtin_choices,clean_grid,preview_prompts,questionnaire_input,table_csv,live_transcript_text
from convolab.llm import Redactor,validate_capabilities
from convolab.prompts import ALLOWLISTS,DEFAULTS
from convolab.presentation import introduction,study_setup_heading
from convolab.questionnaire_ui import questionnaire_editor
from convolab.results import chart_data,measurement_view,state_view,transcript_view,ResultsError
from . import auth,download,repository
from .admission import ServiceSettings
from .checkpoints import cloud_results
from .export import browser_export
from .limits import check_study
from .settings import HostedError
from .storage import OwnedCheckpoints
from .workspace import WIDGET_PREFIX as P,clear_private_state
from . import models

def checkpoint_reader(settings):
    return OwnedCheckpoints(ServiceSettings.from_environment(settings,os.environ))

def safe_error(error,keys=()):
    st.error('Please check the following before continuing.')
    st.code(Redactor(keys).text(str(error)),language=None)

def welcome():
    introduction()
    with st.expander('Getting started',expanded=True):
        st.markdown('''1. **Connect your private workspace** using the button below. Complete the bot check if asked, then wait until you see “Your private workspace is connected.”
2. **Set up a study.** You can create a new study, load an example, or import a saved study setup using a YAML file. When creating a new study, add the shared task instructions, each agent's persona and private role information, and the measurement items you want to collect. Choose a large language model (LLM) for each agent, review the run settings and preview the prompts.
3. **Run it.** Real models need your own provider API key for each agent and may incur charges. Use Run with mock models to try the same study with mock responses, without keys or model charges.
4. **Inspect and download.** Open a saved execution under My results. The research ZIP includes study settings, assigned personas, conversation transcripts, questionnaire responses, and run and error records, plus private states and reactions when enabled. Download it within 24 hours.''')
    with st.expander('Important information'):
        st.markdown('''**Your workspace and data.** Please return using the same browser profile so you can find your saved results. Another browser or device has a separate workspace. If you clear this site's browser data, you lose access to this workspace. On a shared computer, please download your results privately before clearing site data when you finish. Streamlit and Supabase process your study inputs and outputs in the United States. Your selected model providers receive their agents' prompts, and Cloudflare Turnstile provides bot protection.

**API keys and costs.** Please enter your model API keys only in the password fields. Your keys pass through the hosting server, so you need to trust its administrators. A running or paused study execution keeps a temporary copy in memory until it ends. If you lose workspace access, the entry fields are cleared, but accepted work may continue. Your keys are not saved in study files, logs, public files or browser storage. Real API calls may charge your provider account; mock demonstrations make no provider API calls.

**Save your results.** Please download your research ZIP within 24 hours after completion is confirmed or an interruption is detected. Viewing or downloading does not extend that deadline. Your downloaded copies stay on your device. A service fault can delay deletion from the server, but access is still blocked after expiry. Your anonymous workspace account becomes eligible for deletion after seven days without confirmed activity, once its study records and files are gone. Used Start identifiers remain until account deletion to prevent duplicate executions. Your API keys are not kept for seven days.

**Website limits.** You can start a study only when the website's single execution slot is free. If it is busy, please try later; your API key is not queued. Each execution allows up to 300 estimated model requests, including mock requests, 5 MB uploads and a 25 MB result bundle. You have a 30-minute running-time limit per execution, including pauses and all conversations in that execution. This is not a limit on how long you can browse the website. Already-sent API calls may still finish and incur charges after TeamLab stops sending new ones.

**Pause, continue and interruptions.** Click Pause study and wait for “Study paused.” The current message or response group, including retries, finishes first. Then click Continue study to carry on from the same step. Your settings stay locked, the execution slot stays reserved, and paused time counts toward the 30-minute limit. You can continue only while the original server is still running; completed, failed or interrupted executions cannot be continued. Refreshing or reconnecting does not start or continue API calls. Work may continue if your browser disconnects, but a server restart or lost execution ownership interrupts it. Please inspect any partial results and correct key, model-access or quota problems before choosing Prepare to run again.

**Check your results.** Please check the conversation, measurement and saving statuses separately. A finished conversation can still have missing questionnaire answers. If saving fails, you may have only an earlier checkpoint, without later or unfinished records. Please treat simulated answers as exploratory outputs; they do not replace evidence from human studies.''')


def builtin_information():
    with st.expander('Built-in data: source and conditions'):
        st.write('The built-in personas come from public-use ANES 2016 data. Use these data only for research or statistical purposes, cite ANES and its documentation, and do not try to identify respondents. Report any identity you discover to ANES. The data have separate conditions from the software license. Personas are drawn with equal probability, without survey weights, so these draws do not represent the US population.')
        st.markdown('[ANES conditions](https://electionstudies.org/data-center/#terms-of-use) · [ANES 2016 source](https://electionstudies.org/data-center/2016-time-series-study/)')

class EditorUI:
    def __init__(self,settings,identity,workspace,busy):
        self.settings,self.identity,self.workspace,self.busy=settings,identity,workspace,busy
        self.editor,self.revision,self.keys=workspace.editor_copy(identity)
        self.prefix=P+f'editor_{self.revision}_'

    def request(self,kind,*args):
        # Read every current widget before applying an import or freezing work.
        st.session_state[P+'intent']=(kind,args,self.revision)

    def text(self,label,value,key,help,*,area=False):
        return (st.text_area if area else st.text_input)(label,value='' if value is None else str(value),
            key=self.prefix+key,disabled=self.busy,help=help)

    def export(self,data,filename,label,*,disabled=False,key):
        keys=self.workspace.editor_copy(self.identity)[2].values() if filename in {'study.yaml','items.csv'} else ()
        browser_export(self.identity,data,filename,label,keys=tuple(keys),disabled=self.busy or disabled,key=P+key)

    def upload(self,label,types,key,help):
        return st.file_uploader(label,type=types,key=self.prefix+key,disabled=self.busy,max_upload_size=5,
            help=help+' Maximum file size: 5 MB.')

    def render(self):
        e=self.editor
        study_setup_heading()
        st.subheader('1. Create a new study')
        st.write('Start with a blank setup, then add your task, agents, measures and models.')
        st.button('Create a new study',disabled=self.busy,on_click=self.request,args=('new',),help='Open a blank study with the default ANES persona pool. This replaces your current settings and clears entered API keys. No API calls are made.')
        st.subheader('2. Load an example study')
        st.write('Open a prepared study to explore the settings. Choose Run with mock models later to try it with mock responses and no API key.')
        example=st.selectbox('Example study',list(EXAMPLES),key=P+'example',disabled=self.busy,help='Choose an example, then click Load example study to open it.')
        st.button('Load example study',disabled=self.busy,on_click=self.request,args=('example',example),help='Replace the current study and clear entered API keys.')
        st.subheader('3. Import a study file')
        with st.expander('Import study'):
            st.write('Load study settings from a YAML file saved earlier. Choose a .yaml or .yml file, rather than the results ZIP. This loads the setup; it does not resume an earlier run. An invalid file leaves your study unchanged.')
            upload=self.upload('Study YAML file',['yaml','yml'],'study_upload','Choose a study file previously saved by TeamLab, or one using the documented format.')
            st.button('Load uploaded study',disabled=self.busy or upload is None,on_click=self.request,args=('study',upload.getvalue() if upload else b''),help='Check the file before replacing the current study.')
        if not st.session_state.get(P+'show_editor') and not e.settings.get('study_name'):
            self.apply_intent();return None,None,None
        if st.session_state.get(P+'study_editor_origin')=='new':st.subheader('New study')
        e.settings['study_name']=self.text('Study name',e.settings['study_name'],'name','Name for this study and its output records.')
        save_area=st.empty()
        st.header('Task')
        e.settings['task_instructions']=self.text('Task instructions',e.settings['task_instructions'],'task','Task shared by every participant.',area=True)
        st.header('Agents')
        st.subheader('Agent personas')
        builtin_information()
        sources={'builtin':'Use the built-in ANES 2016 personas','custom':'Use my own personas'}
        modes={'fixed':'Same personas every run','pool':'Draw from a persona pool'}
        source=st.radio('Persona source',list(sources),index=list(sources).index(e.persona_source),format_func=sources.get,key=self.prefix+'source',disabled=self.busy,help='Choose the built-in dataset, or enter or upload your own personas.')
        mode=st.radio('Persona selection',list(modes),index=list(modes).index(e.persona_mode),format_func=modes.get,key=self.prefix+'mode',disabled=self.busy,help='Keep the same personas for every conversation, or draw new ones from a pool with equal probability and no reuse within this execution.')
        count=st.selectbox('Number of agents',[2,3],index=len(e.settings['agents'])-2,key=self.prefix+'count',disabled=self.busy,help='Choose two or three participants. Changing this resets the speaker order; fill in any new agent before running.')
        builtin=[]
        if e.persona_source=='custom':
            personas=self.upload('Upload personas (Excel or CSV)',['xlsx','csv'],'personas_upload','For fixed personas, include one row per agent, in agent order. For a pool, give every persona a unique text ID. Excel imports use the first worksheet. Keep IDs as text to preserve leading zeros.')
            st.button('Apply uploaded personas',disabled=self.busy or personas is None,on_click=self.request,args=('personas',personas.getvalue() if personas else b'',personas.name if personas else 'personas.csv'),help='Check and replace persona data. Model settings, private roles and entered keys stay unchanged.')
            self.export(table_csv([],PERSONA_COLUMNS),'personas_template.csv','Download persona template',key='persona_template')
            if e.persona_mode=='pool':
                st.caption(f'Custom pool: {len(e.custom_pool)} rows. Maximum runs: {len(e.custom_pool)//len(e.settings["agents"])}.')
                with st.expander('View uploaded personas',expanded=False):
                    st.dataframe(pd.DataFrame(e.custom_pool),hide_index=True)
        else:
            try:
                builtin=builtin_choices()
                st.caption(f'{len(builtin):,} personas. Source: supplied ANES 2016 dataset and appendix. Uniform unweighted draws are not weighted US population estimates.')
                with st.expander('View built-in personas',expanded=False):
                    search=self.text('Search built-in personas','','builtin_search','Search IDs, demographics or personality values.')
                    st.dataframe(pd.DataFrame([p.model_dump() for p in builtin_choices(search)]),hide_index=True)
                self.export(builtin_bytes(),'anes2016_personas.csv','Download built-in dataset',key='builtin_export')
                if e.persona_mode=='pool':st.info(f'This pool permits at most {len(builtin)//len(e.settings["agents"])} runs; website call limits also apply.')
            except ConfigError as error:safe_error(error,self.keys.values())
        for index,agent in enumerate(e.settings['agents']):
            with st.expander(f'Agent {index+1}',expanded=True):
                for field,label,help in (
                    ('display_name','Display name (optional)','Leave blank to generate a name for the persona. Entered names must be unique, on one line and without a colon.'),
                    ('model','Model','Choose a model for this agent. Real models need a key from the matching provider; mock needs no key.'),
                    ('temperature','Temperature','Controls variation in generated answers. Provider limits apply. If a model cannot use this setting, TeamLab warns you and omits it.'),
                    ('max_tokens','Main response limit','Maximum output tokens for one spoken reply. A token is a short piece of text. Reasoning models may also use this allowance for internal reasoning.'),
                    ('role_instructions','Private role instructions','Visible only to this participant unless it chooses to share them.')):
                    old=agent.get(field,{'temperature':1.0,'max_tokens':400,'role_instructions':''}.get(field))
                    if field=='model':
                        options=models.choices(old)
                        agent[field]=st.selectbox(label,options,index=options.index(old) if old in options else 0,
                            format_func=models.label,key=self.prefix+f'seat{index}_{field}',disabled=self.busy,help=help)
                        st.caption(models.guidance(agent[field]))
                    else:
                        agent[field]=self.text(label,old,f'seat{index}_{field}',help,area=field=='role_instructions')
                    if field=='model' and agent[field]!=old:agent['api_base']=None
                key=self.keys.get(index+1)
                value=st.text_input(f'Agent {index+1} API key',value=key.get_secret_value() if key else '',type='password',autocomplete='off',key=P+f'key_{self.revision}_{index+1}',disabled=self.busy or agent['model']=='mock',help='Paste the full secret API key from this model provider. Each agent uses its own key field; you may use the same provider key for multiple agents.')
                if not self.busy:self.workspace.set_key(self.identity,index+1,value,expected_revision=self.revision)
                st.caption('Keys are kept temporarily in memory and are not saved. This website uses official OpenAI, Anthropic and Google endpoints. Other endpoints are available in the local app.')
                if e.persona_mode=='fixed' and e.persona_source=='custom':
                    for section,fields in (('Demographics',('persona_id',)+DEMOGRAPHIC_FIELDS),('Personality',BIG_FIVE_FIELDS)):
                        with st.expander(section,expanded=False):
                            for field in fields:
                                row=e.custom_fixed[index]
                                row[field]=self.text(field.replace('_',' ').capitalize(),row.get(field),f'persona{index}_{field}',
                                    'Optional persona label. Leading zeros are kept.' if field=='persona_id' else 'Optional personality score from 1 to 5. Leave blank to omit it.' if field in BIG_FIVE_FIELDS else 'Enter age as a whole number.' if field=='age' else 'Enter the demographic description for this persona.')
                elif e.persona_mode=='fixed' and builtin:
                    by_id={p.persona_id:p for p in builtin};choices=[None]+list(by_id);selected=e.builtin_fixed_ids[index]
                    selected=st.selectbox('Built-in persona ID',choices,index=choices.index(selected) if selected in choices else 0,
                        format_func=lambda p:'Choose a persona' if p is None else p+' — '+', '.join(str(getattr(by_id[p],f)) for f in PERSONA_COLUMNS[1:]),key=self.prefix+f'builtin_id_{index}',disabled=self.busy,help='Choose a different persona ID for each agent. Use the search above to inspect matching personas.')
                    e.builtin_fixed_ids[index]=selected
                    if selected:
                        with st.expander('View selected persona',expanded=False):
                            st.dataframe(pd.DataFrame([by_id[selected].model_dump()]),hide_index=True)
        self.keys=self.workspace.editor_copy(self.identity)[2]
        st.button('Clear model keys',disabled=self.busy,on_click=self.request,args=('clear_keys',),help='Clear all model-key fields and remove their temporary server copies. This is unavailable while a study is running or paused.')
        with st.expander('Choosing a model'):
            st.write('Please choose a model separately for each agent. The list contains text models whose request formats this version of TeamLab supports. Your provider account still needs access to the model and sufficient API quota. Please check the selected model’s note for restrictions and response-limit guidance. Image, audio and tool-only models are not included.')
            st.markdown('[OpenAI models](https://developers.openai.com/api/docs/models) · [Anthropic models](https://platform.claude.com/docs/en/models/overview) · [Google models](https://ai.google.dev/gemini-api/docs/models)')
        st.header('Questionnaire')
        e.settings['items'],questionnaire_redraw=questionnaire_editor(e.settings.get('items',[]),
            revision=self.revision,prefix=self.prefix,baseline_key=P+'questionnaire_input',disabled=self.busy)
        items=self.upload('Upload questionnaire CSV',['csv'],'items_upload','Use all the column headers shown in the downloaded questionnaire CSV. The file is checked before replacing the table.')
        st.button('Apply uploaded questionnaire',disabled=self.busy or items is None,on_click=self.request,args=('items',items.getvalue() if items else b'',items.name if items else 'items.csv'),help='Validate and replace only the questionnaire items. Your task, agents and entered API keys stay unchanged.')
        self.export(table_csv(e.settings['items'],ITEM_COLUMNS),'items.csv','Download questionnaire CSV',key='items_export')
        instructions=e.settings.setdefault('survey_instructions',dict(SURVEY_INSTRUCTIONS))
        for phase in ('pre','turn','post'):instructions[phase]=self.text({'pre':'Before-conversation survey instructions','turn':'During-conversation survey instructions','post':'After-conversation survey instructions'}[phase],instructions.get(phase,SURVEY_INSTRUCTIONS[phase]),'instructions_'+phase,'Instructions shown to each agent alongside the questions '+{'pre':'before','turn':'during','post':'after'}[phase]+' the conversation. These instructions do not change which questions are scheduled.',area=True)
        st.header('Run settings');run=e.settings['run']
        st.caption('A run is one independent conversation. We recommend starting with 1 run. One conversation turn is one agent’s chance to speak; a pass also uses a turn. A full round gives every agent one turn.')
        for field,label,help in (('n_runs','Number of runs','We recommend starting with 1. Each run is an independent conversation with fresh participant states and conversation history. All runs share this execution’s 30-minute limit.'),('max_turns','Maximum conversation turns','Maximum turns per conversation, across all agents. Each spoken message or pass uses one turn; a turn is not a full round.'),('measure_every_n_turns','Survey every N conversation turns','Ask during-conversation questions after a public event at turns N, 2N, 3N and so on. A hidden pass at a scheduled turn does not trigger or postpone that survey.')):
            run[field]=st.number_input(label,min_value=1,value=int(run[field]),step=1,key=self.prefix+field,disabled=self.busy,help=help)
        order=self.text('Speaker order',', '.join(str(v) for v in run['speaker_order']),'speaker_order','List each agent number once, separated by commas. For example: 1, 2, 3.')
        run['speaker_order']=[p.strip() for p in order.split(',')]
        for field,label,help in (('shuffle_order_each_run','Shuffle speaker order each run','Randomize the speaking order once at the start of each conversation, then repeat that order.'),('allow_pass','Allow pass','Let an agent respond with exactly [PASS] instead of speaking. A pass still uses one conversation turn. Consecutive passes from all agents automatically end the conversation.')):
            run[field]=st.checkbox(label,value=run[field],key=self.prefix+field,disabled=self.busy,help=help)
        run['silence_visibility']=st.selectbox('Silence visibility',['public','hidden'],index=['public','hidden'].index(run['silence_visibility']),key=self.prefix+'visibility',disabled=self.busy or not run['allow_pass'],help='Public passes are visible to all agents, update enabled private states and can trigger questionnaires. Hidden passes appear only in researcher records. Turning off Allow pass keeps this setting for later.')
        for field,label,help in (('allow_early_end','Allow early ending','Let agents agree to end early by placing [END] at the end of their spoken replies. Every agent must do so in the same completed round; earlier confirmations do not carry forward. These replies remain in the conversation. Consecutive passes can also end it when Allow pass is enabled.'),('remember_pre_survey','Remember own pre-conversation survey responses','Let each agent see only its own valid pre-conversation survey responses in later speech and questionnaire prompts. Other agents cannot see them. These responses are not included in private-state update prompts.'),('randomize_item_order','Randomize questionnaire order','Shuffle question order separately for each agent and each questionnaire administration. Question IDs stay unchanged in the results.'),('inner_reaction','Collect private reactions','Ask each agent for a private reaction with each nonempty questionnaire. You can inspect reactions in the results; agents do not see or remember them later.'),('private_state','Retain each agent’s private state during the conversation','Initialize a separate simulated state for each agent and update all agents’ states after every spoken message or visible silence, before further questionnaires or speech. Each agent sees only its own state. Hidden passes do not update states.')):
            run[field]=st.checkbox(label,value=run[field],key=self.prefix+field,disabled=self.busy,help=help)
        run['seed']=self.text('Seed (optional)',run.get('seed'),'seed','Optional whole number to reproduce persona draws, names, ordering and mock answers. Leave blank to generate and save one. Real-model answers may still vary.')
        with st.expander('Advanced'):
            st.caption('Please inspect the prompts here if you need to check exactly what agents are told. The defaults implement the study design. Edit them only deliberately, and keep required placeholders. Preview prompts to see an assembled example; previews make no API calls.')
            for field in ('state_max_tokens','survey_max_tokens'):run[field]=st.number_input('Private-state response limit (tokens)' if field=='state_max_tokens' else 'Questionnaire response limit (tokens)',min_value=1,value=int(run[field]),step=1,key=self.prefix+field,disabled=self.busy,help='Maximum output tokens for one '+('private-state response' if field=='state_max_tokens' else 'agent’s questionnaire response, covering all its questions and any reaction')+'. Retries use the same limit. Reasoning models may use some of it for internal reasoning.')
            templates=e.settings.setdefault('prompt_templates',{})
            for field,default in DEFAULTS.items():
                value=self.text(field,templates.get(field,default),'template_'+field,'Prompt template or text fragment. Keep these required placeholders: '+(', '.join(sorted(ALLOWLISTS[field])) or 'none')+'. Do not add other placeholders. Use Preview prompts to inspect the assembled message.',area=True)
                # Merely displaying a default must not rewrite a supplied study.
                if field in templates or value!=default:templates[field]=value
        if not self.busy and not self.workspace.commit_editor(self.identity,e,self.revision):st.rerun()
        if questionnaire_redraw and not self.busy:st.rerun()
        if not self.busy and (source,mode,count)!=(e.persona_source,e.persona_mode,len(e.settings['agents'])):
            self.workspace.select_personas(self.identity,source,mode,count);st.session_state.pop(P+'intent',None);st.session_state.pop(P+'preview',None);st.rerun()
        self.apply_intent()
        study=demo=error=None
        try:
            study=self.workspace.study(self.identity)
            for warning in dict.fromkeys(w for p in validate_capabilities(study) for w in p.warnings):st.warning(warning)
            check_study(study)
        except (ConfigError,HostedError) as exc:error=exc
        try:demo=self.workspace.study(self.identity,demo=True);check_study(demo)
        except (ConfigError,HostedError):pass
        with save_area.container():self.export(self.workspace.yaml(self.identity) if study else b'','study.yaml','Save study (YAML)',disabled=study is None,key='save_study')
        if study:
            with st.expander('Study YAML without keys'):st.code(self.workspace.yaml(self.identity),language='yaml')
        seat=st.selectbox('Preview seat',list(range(1,len(e.settings['agents'])+1)),key=self.prefix+'preview_seat',disabled=self.busy,help='Choose the agent whose prompts you want to inspect. Previewing does not make model requests or affect later persona draws.')
        if st.button('Preview prompts',disabled=self.busy or demo is None,help='Show this agent’s prompts using sample state and answers where needed. No API calls are made.') and not self.busy and demo is not None:
            st.session_state[P+'preview']=(self.workspace.yaml(self.identity),preview_prompts(study,seat))
        preview=st.session_state.get(P+'preview')
        if preview and study and preview[0]==self.workspace.yaml(self.identity):
            result=Redactor(self.keys.values()).value(preview[1]);st.info(result['note']);st.text(f'Resolved example: seat {result["seat"]}, {result["name"]}');st.json(result['persona'])
            if result['sample_state']:st.caption('Sample state — not a generated result');st.json(result['sample_state'])
            for block in result['blocks']:
                with st.expander(block['label']):st.code(block['system'],language=None);st.code(block['user'],language=None)
        return study,demo,error

    def apply_intent(self):
        requested=st.session_state.pop(P+'intent',None)
        if requested is None or self.busy:return
        kind,args,revision=requested
        if revision!=self.workspace.revision:st.warning('The editor changed before that action. Review and click again.');return
        try:
            if kind=='new':
                self.workspace.new_study(self.identity)
                st.session_state[P+'show_editor']=True
            elif kind=='example':self.workspace.load_example(self.identity,*args)
            elif kind=='study':self.workspace.replace_upload(self.identity,*args)
            elif kind in {'personas','items'}:self.workspace.import_table(self.identity,kind,*args)
            elif kind=='clear_keys':self.workspace.clear_keys();self.workspace.revision+=1;st.session_state[P+'clear_key_widgets']=True
            if kind in {'new','example','study'}:st.session_state[P+'study_editor_origin']=kind
            st.session_state.pop(P+'preview',None);st.rerun()
        except (ConfigError,HostedError) as error:safe_error(error,self.keys.values())

def render_results(view,execution,*,website_status=None):
    if view.study.get('study_name'):st.text('Study: '+str(view.study['study_name']))
    failed=sum(r.get('status')=='failed' for r in view.tables['runs'])
    label='Research execution'
    if website_status=='interrupted':
        label='Research status at last saved checkpoint'
        st.caption('Website execution is interrupted. The research status below belongs to the last saved checkpoint; this execution cannot resume.')
    st.text(f'{label}: {view.metadata.get("status","unknown")}. Failed Runs: {failed}. Frozen silence visibility: {view.visibility}. Persona source: {view.persona_source}.')
    st.caption('Execution completion means requested Runs were finalized, including failed Runs; it does not establish conversation success, measurement completeness or psychological accuracy.')
    st.text('Saved records verified complete: '+('Yes' if view.files_complete else 'No / unverified'))
    st.text('Measurements complete: '+('Yes' if view.measurements_complete else 'No / unverified'))
    for warning in view.warnings:st.warning('Retained-data warning');st.code(warning,language=None)
    for error in view.errors:st.error('Retained execution error');st.code(error.replace(str(view.folder),'execution '+execution),language=None)
    runs=[r['run_id'] for r in view.tables['runs'] if r.get('run_id')]
    if not runs:return
    labels={run_id:f'Conversation {n+1} — {next(r for r in view.tables["runs"] if r["run_id"]==run_id).get("status","unknown")} — {run_id}' for n,run_id in enumerate(runs)}
    chosen=st.selectbox('Run to inspect',runs,format_func=labels.get,key=P+'result_run_'+execution,help='Each run is one independent conversation. Choose its number to view the transcript and measurements. The full ID identifies its output records.')
    row=next(r for r in view.tables['runs'] if r['run_id']==chosen)
    st.text(f'Conversation: {row.get("status","unknown")}; ending: {row.get("end_reason","unknown")}.')
    st.subheader('Transcript');st.caption('Visible silence is a public event. Hidden passes are researcher-only, outside participant context.')
    st.dataframe(pd.DataFrame(transcript_view(view,chosen)),hide_index=True)
    st.subheader('Questionnaire measurements');st.dataframe(pd.DataFrame(measurement_view(view,chosen)),hide_index=True)
    if view.tables['reactions']:
        st.caption('Private reactions — researcher only');st.dataframe(pd.DataFrame([r for r in view.tables['reactions'] if r.get('run_id')==chosen]),hide_index=True)
    likert=[i['item_id'] for i in view.study.get('items',[]) if i.get('type')=='likert']
    if likert:
        item=st.selectbox('Likert question',likert,key=P+'result_item_'+execution,help='Choose a rating question to plot each agent’s answers. Before, during and after measurements are shown separately; the table includes their timing.')
        chart=chart_data(view,chosen,item)
        if not chart.empty:
            plotted=chart.rename_axis('Administration').reset_index().melt(id_vars=['Administration'],var_name='Agent',value_name='Response')
            st.altair_chart(alt.Chart(plotted).mark_line().encode(x=alt.X('Administration:N',sort=list(chart.index)),y=alt.Y('Response:Q'),color=alt.Color('Agent:N')));st.dataframe(chart)
    uncommitted=view.uncommitted.get(chosen,[])
    if uncommitted:st.error(f'Public events {uncommitted} have no complete state commitment. Earlier snapshots are historical, not current for those events.')
    if view.study.get('run',{}).get('private_state',True):
        st.subheader('Private states — researcher only');versions=view.versions.get(chosen,[])
        if versions:
            seat=st.selectbox('State seat',sorted({s['seat'] for s in view.states if s['run_id']==chosen}),key=P+'state_seat_'+execution,help='Choose the agent whose private state you want to inspect.')
            version=st.selectbox('Committed version',versions,index=len(versions)-1,key=P+'state_version_'+execution,help='Choose a saved state version. Versions are available only after every agent’s state passes validation. An earlier version describes an earlier point in the conversation.')
            state=state_view(view,chosen,seat,version);s=state['snapshot']
            st.text(f'Seat {seat} ({s["agent"]}), version {version}; public event {s["public_event_index"]} ({s["event_type"]}); spoken messages {s["public_message_index"]}; after opportunity {s["after_turn"]}.')
            st.json(s['state']);st.dataframe(pd.DataFrame(state['related_surveys']),hide_index=True)
            if state.get('related_reactions'):st.dataframe(pd.DataFrame(state['related_reactions']),hide_index=True)
        else:st.info('No complete state version is available for this Run.')

def verified_fragment(settings,message,identity):
    try:
        current=auth.AuthVerifier(settings).verify(message)
        if current.owner_id!=identity.owner_id:raise HostedError('Reconnect your private workspace.')
        return current
    except HostedError:clear_private_state(st.session_state);st.rerun(scope='app')


def live_controls(runtime,identity,execution,view=None,*,columns=None):
    state=(view or {}).get('control_state','running')
    active=bool(execution and view and not view['done'])
    pause_allowed=active and state=='running'
    continue_allowed=active and state=='paused'
    pause_column,continue_column=columns if columns is not None else st.columns(2)
    with pause_column:
        pause=st.button('Pause study',key=P+'pause',disabled=not pause_allowed,
            help='Pause after the current message or response group, including retries, finishes. Wait for Study paused. Settings stay locked, API keys stay temporarily in memory and the execution slot stays reserved. Paused time counts toward the 30-minute limit.')
    with continue_column:
        resume=st.button('Continue study',key=P+'continue',disabled=not continue_allowed,
            help='Continue the same paused execution without repeating completed requests. This requires its original server. Completed, failed, timed-out or interrupted studies cannot be continued. Refreshing does not continue work.')
    if (pause and pause_allowed) or (resume and continue_allowed):
        try:
            (runtime.pause if pause else runtime.continue_study)(identity,execution)
            st.rerun(scope='app')
        except HostedError as error:st.warning(str(error))

def render_interface(settings,message,identity,workspace,runtime):
    rows=()
    try:rows=repository.OwnedRepository(settings).list_executions(identity)
    except HostedError as error:st.warning(str(error))
    busy=any(r.status in {'pending','running'} for r in rows)
    identifier=st.session_state.get(P+'active_execution');control_view=None
    if runtime and identifier:
        try:control_view=runtime.observe(identity,identifier);busy=busy or not control_view['done']
        except HostedError:pass
    ui=EditorUI(settings,identity,workspace,busy);study,demo,validation=ui.render()
    st.header('Run');ready=False
    st.write('Review your settings, then click Start to run the study with your selected models. To try the workflow without API keys or model charges, choose Run with mock models. You can pause an active study and continue it while the same server is running.')
    if validation:safe_error(validation,ui.keys.values())
    if study:
        try:
            estimate=check_study(study)
            st.info(f'Maximum-length estimate: {estimate["per_run"]} model requests per conversation; {estimate["total"]} for this execution. Up to {estimate["retry_ceiling"]} attempts including retries. Real-model requests are API calls; mock requests stay on this server. Passes, early endings and failures can reduce the total.')
            mode='Mock only' if all(a.model=='mock' for a in study.agents) else 'Real model or mixed mock/real'
            st.text('Selected model mode: '+mode+'. Mock requests have no API cost; real models may charge your provider account.')
            workspace.freeze(identity);ready=validation is None
        except (ConfigError,HostedError) as error:safe_error(error,ui.keys.values())
    st.caption('Website limits: one active study execution, 300 estimated model requests, 5 MB uploads and a 25 MB result bundle. The 30-minute running-time limit covers all conversations in an execution and includes paused time. Already-sent API calls may still finish and incur charges.')
    nonce=P+'start_nonce';st.session_state.setdefault(nonce,str(uuid4()))
    if runtime:
        control_columns=st.columns(3)
        for label,kind,valid in [('Start','start',ready),('Run with mock models','demo',demo is not None)]:
            with control_columns[0] if kind=='start' else st.container():
                clicked=st.button(label,disabled=busy or not valid,help='Start a new execution with your current settings, selected models and API keys. Real API calls may charge your provider account.' if kind=='start' else 'Run a copy of your current study with mock responses. No provider API calls or API key are needed. Your selected models stay unchanged.')
            if clicked and not busy and valid:
                try:
                    answer=runtime.start(identity,st.session_state[nonce],workspace,**({'demo':True} if kind=='demo' else {}))
                    if answer['outcome'] in {'accepted','existing','duplicate'} and answer['execution_id']:
                        st.session_state[P+'active_execution']=answer['execution_id'];st.info('Viewing accepted work. Refresh never starts it again.');st.rerun()
                    elif answer['outcome']=='duplicate':st.warning('This start has already been used. Click Prepare to run again before starting another study execution.')
                    elif answer['outcome']=='limit':st.warning('The study exceeds the website call limit. Reduce settings explicitly before a new action.')
                    else:st.warning('The website is busy. Your API keys were not queued. When it is available, click Prepare to run again, then choose how to run.')
                except (ConfigError,HostedError) as error:safe_error(error,ui.keys.values())
            if kind=='start':live_controls(runtime,identity,identifier,control_view,columns=control_columns[1:])
            if kind=='demo':st.caption('Runs your current setup with mock responses. No provider API calls, API key or API charges. Your selected models stay unchanged.')
        if st.button('Prepare to run again',disabled=busy,help='Allow a new start after a finished/failed run or a busy response. Then click Start or Run with mock models. This button starts no work and preserves your settings.'):
            st.session_state[nonce]=str(uuid4());st.rerun()
        st.caption('Use this after a finished or failed run, or a busy response, to allow another start. Then click Start or Run with mock models. This does not run anything or reset your settings.')
    else:st.info('Execution is disabled while website services are prepared. Settings and previews send no model requests.')
    if runtime:
        @st.fragment(run_every='2s')
        def progress():
            current=verified_fragment(settings,message,identity);execution=st.session_state.get(P+'active_execution')
            if not execution:return
            try:
                view=runtime.observe(current,execution)
                # Redraw the main row only when its enabled state changes.
                # Start stays outside the fragment so it reads same-click edits.
                if view.get('control_state')!=(control_view or {}).get('control_state'):st.rerun(scope='app')
                if not view['done']:
                    control=view.get('control_state','running')
                    if control=='paused':st.info('Study paused. Click Continue study to carry on from the same step. No model requests are sent while paused.')
                    elif control=='pausing':st.info('Pause requested. The current message or response group, including any retries, will finish before the study pauses.')
                    elif control=='continuing':st.info('Continuing the same study execution. Completed requests will not be repeated.')
                    elif control=='stopping':st.info('The execution is stopping. Waiting for outstanding work and saving to settle.')
                    elif control=='finishing':st.info('The study is finishing and saving its results.')
                    else:st.info('Your execution is running. Refresh will not restart it.')
                    live=runtime.read_live(current,execution);c=live['context'];limits=live['settings'];n=c.get('number',1);turn=c.get('turn',0);total=limits['n_runs'];maximum=limits['max_turns']
                    stage={'state_init':'Initializing private states','state_update':f'Updating private states after turn {turn}','survey':'Collecting surveys','main':'Waiting for the next participant'}.get(c.get('call_type'),'Starting')
                    st.progress(min(1.0,((n-1)+min(turn,maximum)/maximum)/total),text=f'Run {n} of {total}, turn {turn} of {maximum} — {stage}')
                    st.text(live_transcript_text(live['events'],limits['silence_visibility']))
                    for error in live['errors']:st.warning('A research administration failed or has missing data.');st.code(error,language=None)
                elif not view['outcome_recorded']:st.warning('Final outcome is unconfirmed. This execution will not restart automatically. Refresh My results when the service returns.')
                elif view['status']=='complete':st.success(f'Execution finished: {view["n_runs_finalized"]} '+('conversation' if view['n_runs_finalized']==1 else 'conversations')+f' finalized, {view["n_failed_runs"]} failed. Please check My results for measurement completeness.')
                else:
                    reasons={'time_limit':'The 30-minute limit was reached.','output_limit':'The output-size limit was reached.','ownership_lost':'The server could no longer confirm ownership.','cloud_save_failed':'Cloud saving failed.'}
                    st.error(reasons.get(view['hosting_reason'],'The execution stopped because of an error.')+' No later requests will start. Inspect the last verified partial checkpoint before preparing to run again.')
                st.caption({'saved':'Latest private checkpoint verified.','failed':'Cloud saving failed. Last verified data may be incomplete.','pending':'Private saving has not been confirmed yet.','unavailable':'Private saving is unavailable in this test configuration.'}[view['save_status']])
                if view['done'] and st.session_state.get(P+'key_revision')!=workspace.revision:st.rerun(scope='app')
            except HostedError as error:st.warning(str(error))
        progress()
    st.header('Results');st.subheader('My results')
    st.write('Each Start or mock start creates one study execution, which can contain several independent conversations. Choose an execution below, then open its saved records or download its research ZIP. A finished execution can still contain failed conversations or missing measurements.')
    if st.button('Refresh results',help='Reload your saved-results list, statuses and opened results. This does not start or continue a study or extend the download deadline.'):st.rerun()
    if not rows:st.info('No saved executions in this private workspace yet. Load an example and run a mock demonstration when enabled.')
    rows=tuple(sorted(rows,key=lambda row:(row.created_at,row.id),reverse=True))
    selected=None
    ids=[row.id for row in rows]
    if rows:
        status_labels={'pending':'Waiting to start','running':'Active','complete':'Finished','failed':'Failed','interrupted':'Interrupted'}
        filters={'All':lambda row:True,'Active':lambda row:row.status in {'pending','running'},
                 'Finished':lambda row:row.status=='complete','Failed or interrupted':lambda row:row.status in {'failed','interrupted'}}
        choice=st.selectbox('Filter executions',list(filters),key=P+'results_filter',help='Filter the executions in your private workspace. Finished describes execution status; check measurements separately after opening the records.')
        visible=[row for row in rows if filters[choice](row)]
        st.caption(f'{len(visible)} of {len(rows)} executions shown, newest first. All dates and times below are UTC. Refresh results if a status looks outdated.')
        with st.expander('Execution overview'):
            st.dataframe(pd.DataFrame([{'Started (UTC)':row.created_at.strftime('%Y-%m-%d %H:%M'),
                'Execution ID':row.id,'Website status':status_labels[row.status],
                'Saving':{'pending':'Not confirmed','saved':'Checkpoint saved','failed':'Save failed'}[row.save_status],
                'Access expires (UTC)':row.expires_at.strftime('%Y-%m-%d %H:%M') if row.expires_at else 'Not yet set'} for row in visible]),hide_index=True)
        if visible:
            by_id={row.id:row for row in visible}
            selected=st.selectbox('Open existing results',list(by_id),key=P+'selected_result',
                format_func=lambda identifier:by_id[identifier].created_at.strftime('%Y-%m-%d %H:%M UTC')+' · '+status_labels[by_id[identifier].status]+' · '+identifier,
                help='Select an execution by start time, website status and ID. This does not fetch its research ZIP, restore API keys or restart work.')
            row=by_id[selected]
        else:st.info('No executions match this filter. Choose All to see your other executions.')
    if selected:
        st.text(f'Execution ID: {row.id}')
        st.text(f'Website outcome: {status_labels[row.status]}. Saving: '+{'pending':'Not confirmed','saved':'Checkpoint saved','failed':'Save failed'}[row.save_status]+'.')
        if row.status in {'pending','running'}:st.caption('Active may include a paused execution. Use View progress to check its current live state; this does not start or continue work.')
        if row.expires_at:st.caption('Please download before '+row.expires_at.strftime('%Y-%m-%d %H:%M UTC')+'. Viewing or downloading does not extend this deadline.')
        if row.object_key and row.status in {'complete','failed','interrupted'}:
            if row.save_status!='saved' or row.status!='complete':st.warning('This download contains retained partial or failed results. Later or in-flight records may be absent.')
            download.browser_download(settings,identity,row.id)
        if runtime and row.status in {'pending','running'} and st.button('View progress',key=P+'view_'+row.id,help='Check the original execution’s progress. This does not restart interrupted work.'):
            st.session_state[P+'active_execution']=row.id;st.rerun()
        if row.object_key:
            if st.button('Open selected results',help='Read the saved transcript and measures for this execution. This makes no model API calls.'):st.session_state[P+'opened_result']=selected
        else:st.info('No saved research checkpoint is available for this execution yet. Refresh results to check again.')
    opened=st.session_state.get(P+'opened_result')
    readable={row.id for row in rows if row.object_key}
    if opened and opened==selected and opened in readable:
        @st.fragment
        def inspect():
            current=verified_fragment(settings,message,identity)
            try:
                row,data=checkpoint_reader(settings).read(current,opened)
                with cloud_results(data,row,Path('deploy/private/views')) as view:render_results(view,opened,website_status=row.status)
            except (HostedError,ResultsError,OSError) as error:safe_error(error)
        inspect()
    elif opened and opened not in readable:
        st.session_state.pop(P+'opened_result',None);st.warning('Selected results are no longer accessible. Access may have expired; downloaded copies remain yours.')
    st.caption('Connecting, importing, previewing, viewing and downloading never starts or continues model API calls. Private ZIP transfers recheck your identity, ownership and expiry.')
