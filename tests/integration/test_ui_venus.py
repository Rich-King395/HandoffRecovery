"""Shared UI-Venus requests and real recovery lifecycle, using synthetic I/O."""
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handoff_recovery.core.types import LocalInstruction
from handoff_recovery.executor import ExecutorSpec, create_executor, executor_adapter_names
from handoff_recovery.executor.ui_venus import UIVenusExecutor, UIVenusConfiguration, UIVenusExecutorError
from handoff_recovery.integrations.handoffbench import load_ui_venus_bindings, HandoffRecoveryAgent
from tests.integration import test_native_execution_parity as parity
from tests.integration.test_runner_bridge import Planner, execute_plan, verify_plan
from tests.unit.test_coordinator import frame
from tests.unit.test_budget_entrypoints import script


def raw(action='PressBack()', thought='Synthetic own reasoning.'):
    return f'<think>{thought}</think>\n<action>{action}</action>'


@unittest.skipUnless(os.environ.get('HANDOFFBENCH_ROOT'), 'Set HANDOFFBENCH_ROOT for native UI-Venus integration')
class UIVenusIntegrationTests(unittest.TestCase):
    run_agent = parity.NativeExecutionParityTests.run_agent

    def setUp(self):
        self.root=Path(os.environ['HANDOFFBENCH_ROOT']).resolve()
        self.bindings=load_ui_venus_bindings(self.root)
        self.executor=UIVenusExecutor(self.root,UIVenusConfiguration())
        self.executor.start_task('synthetic','Set test option')

    def instruction(self,scope='original',**kwargs):
        if scope=='execution': kwargs.setdefault('handoff',execute_plan().plan.handoff)
        if scope=='verification': kwargs.setdefault('verification',verify_plan().plan.verification)
        return LocalInstruction('Set test option',(),scope,completion_policy='native',condition_updates_enabled=False,**kwargs)

    def test_registry_provider_configuration_and_unsupported_options(self):
        self.assertIn('ui_venus',executor_adapter_names())
        config=self.bindings.api.UIVenusConfiguration(model='synthetic-venus',max_tokens=12000)
        spec=ExecutorSpec(agent='ui_venus',model='ignored',configuration_source='handoffbench_provider_validate_configuration')
        with patch.object(self.bindings.api,'validate_configuration',return_value=config):
            executor=create_executor('ui_venus',self.root,spec)
        self.assertEqual(executor.descriptor['model'],'synthetic-venus')
        self.assertEqual(executor.descriptor['max_tokens'],12000)
        for change in ({'enable_completion_check':True},{'include_recovery_constraints':True},{'include_recovery_uncertain':True},{'agent':'holo'}):
            with self.subTest(change=change),self.assertRaises(ValueError): create_executor('ui_venus',self.root,replace(spec,**change))

    def test_baseline_and_executor_have_identical_native_requests_and_actions(self):
        from resumebench.agents.ui_venus import UIVenusAgent
        api=self.bindings.api;baseline=UIVenusAgent(api.UIVenusConfiguration())
        captures=[]
        def request(path,goal,width,height,*,configuration,history):
            image=api.encode_screenshot(Path(path).read_bytes(),width,height)
            captures.append(api.build_messages(goal,history,image))
            return raw('Click(point=(999,500))')
        public=frame('synthetic')
        with tempfile.TemporaryDirectory() as directory,patch.object(api,'request_action',side_effect=request):
            p=Path(directory)/'public.png';p.write_bytes(public.png)
            observation={'screenshot':str(p),'width':2,'height':2,'xml':'PRIVATE_MUST_NOT_REACH_MODEL'}
            for index in range(5):
                action=baseline.decide(observation,'Set test option',[] if index==0 else [{'action':'tap'}])
                self.assertEqual(action,self.executor.decide(public,self.instruction()))
                self.assertEqual(captures[-2],captures[-1])
        self.assertEqual(len(self.executor._context),5)
        self.assertNotIn('PRIVATE_MUST_NOT_REACH_MODEL',json.dumps(captures))

    def test_isolated_scopes_keep_native_reasoning_images_and_no_feedback(self):
        api=self.bindings.api;calls=[]
        def request(path,goal,*size,configuration,history):
            calls.append({'history':deepcopy(history),'goal':goal})
            return raw()
        with patch.object(api,'request_action',side_effect=request):
            self.executor.decide(frame('prefix'),self.instruction())
            self.executor.decide(frame('verify'),self.instruction('verification'))
            self.executor.decide(frame('execute'),self.instruction('execution',last_action_feedback={'private_sentinel':'NO_FEEDBACK'}))
            self.executor.decide(frame('execute2'),self.instruction('execution'))
            self.executor.decide(frame('fallback'),self.instruction('fallback',resume_original=True))
        self.assertEqual([len(c['history']) for c in calls],[0,0,0,1,1])
        self.assertEqual(calls[3]['history'][0]['raw_response'],raw())
        self.assertTrue(calls[3]['history'][0]['image'])
        self.assertNotIn('NO_FEEDBACK',json.dumps(calls));self.assertNotIn('Last Action Feedback',json.dumps(calls))
        self.assertIn('Do not add condition_updates or completion_check fields.',calls[2]['goal'])
        self.assertNotIn('Report newly satisfied conditions',calls[2]['goal'])
        self.executor.start_task('next','Set test option');self.assertEqual(self.executor._contexts,{'original':[]})

    def test_error_categories_no_retries_and_no_invalid_history(self):
        for response,category in [('incomplete','native_parse_error'),(raw('Click(point=(1000,0))'),'action_mapping_error')]:
            with patch.object(self.bindings.api,'request_action',return_value=response) as request,self.assertRaises(UIVenusExecutorError) as caught:
                self.executor.decide(frame('error'),self.instruction())
            self.assertEqual(caught.exception.category,category);self.assertTrue(caught.exception.recoverable)
            self.assertEqual(self.executor._context,[]);request.assert_called_once()
        with patch.object(self.bindings.api,'request_action',side_effect=RuntimeError('private response')) as request,self.assertRaises(UIVenusExecutorError) as caught:
            self.executor.decide(frame('error'),self.instruction())
        self.assertEqual(caught.exception.category,'request_error');self.assertFalse(caught.exception.recoverable)
        self.assertNotIn('private response',str(caught.exception));request.assert_called_once()
        with patch.object(self.bindings.api,'request_action') as request,self.assertRaises(ValueError):
            self.executor.decide(frame('error'),replace(self.instruction('execution'),condition_updates_enabled=True))
        request.assert_not_called()

    def test_mocked_runner_shared_budget_and_native_completion_independent_score(self):
        for mode in ('no_takeover','takeover'):
            for score in ('false','true'):
                executor=UIVenusExecutor(self.root,UIVenusConfiguration())
                planner=Planner([execute_plan()])
                bridge=HandoffRecoveryAgent(bench_path=self.root,planner=planner,executor=executor,
                    completion_policy='native',condition_updates_enabled=False)
                with tempfile.TemporaryDirectory() as directory,patch.object(self.bindings.api,'request_action',side_effect=[raw(),raw('Finished()')]) as request:
                    result,device=self.run_agent(bridge,Path(directory)/'run',mode=mode,budget=30,prefix_decisions=1,goal_status=score)
                self.assertEqual(result['termination_reason'],'agent_done')
                self.assertEqual(result['task_success'],score=='true')
                self.assertEqual(sum(result['counts']['decisions'].values()),2)
                self.assertEqual(request.call_count,2)
                self.assertEqual(len(planner.calls),1 if mode=='takeover' else 0)
                if mode=='takeover':
                    self.assertEqual(result['counts']['takeover_actions'],1)
                    self.assertEqual(bridge.report()['condition_status'][0]['status'],'pending')
        planner=Planner([execute_plan()]);bridge=HandoffRecoveryAgent(bench_path=self.root,planner=planner,
            executor=UIVenusExecutor(self.root,UIVenusConfiguration()),completion_policy='native',condition_updates_enabled=False)
        with tempfile.TemporaryDirectory() as directory,patch.object(self.bindings.api,'request_action',side_effect=['invalid']+[raw()]*29) as request:
            result,device=self.run_agent(bridge,Path(directory)/'run',mode='takeover',budget=30,prefix_decisions=1)
        self.assertEqual(result['termination_reason'],'budget_exhausted');self.assertEqual(request.call_count,30)
        self.assertEqual(sum(result['counts']['decisions'].values()),30)
        self.assertEqual(result['counts']['takeover_actions'],1);self.assertEqual(len(planner.calls),1)

    def test_native_verification_completion_returns_once_to_planner(self):
        planner=Planner([verify_plan(),execute_plan()])
        bridge=HandoffRecoveryAgent(bench_path=self.root,planner=planner,executor=self.executor,
            completion_policy='native',condition_updates_enabled=False)
        with tempfile.TemporaryDirectory() as directory,patch.object(self.bindings.api,'request_action',side_effect=[raw('Finished()')]*2) as request:
            result,device=self.run_agent(bridge,Path(directory)/'run',mode='takeover',budget=30,goal_status='true')
        self.assertEqual(result['termination_reason'],'agent_done');self.assertEqual(request.call_count,2)
        self.assertEqual(len(planner.calls),2);self.assertEqual(len(device.public_actions),0)

    def test_smoke_cli_and_batch_dry_run_are_explicit_without_io(self):
        smoke=script('run_live_smoke')
        args=['--bench-root',str(self.root),'--baseline','/unused','--device','synthetic','--adb','/unused',
              '--appium-url','http://appium.invalid','--local-agent','ui_venus','--local-model','UI-Venus-2-9B',
              '--local-base-url','http://model.invalid/v1','--mode','no_takeover','--completion-policy','native','--condition-updates','disabled']
        self.assertEqual(smoke.parse_args(args).local_max_output_tokens,16384)
        self.assertEqual(smoke.parse_args(args+['--local-max-output-tokens=1024']).local_max_output_tokens,1024)
        from contextlib import redirect_stderr,redirect_stdout
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit): smoke.parse_args(args+['--condition-updates','enabled'])
        batch=script('run_recovery_batch')
        values={'RECOVERY_LOCAL_AGENT':'ui_venus','RECOVERY_PLANNER_PROVIDER':'synthetic',
                'RECOVERY_PLANNER_SYNTHETIC_ENDPOINT':'http://planner.invalid/v1/chat/completions',
                'RECOVERY_PLANNER_SYNTHETIC_MODEL':'synthetic','RECOVERY_PLANNER_SYNTHETIC_API_KEY':'synthetic',
                'UI_VENUS_BASE_URL':'http://model.invalid/v1','UI_VENUS_MODEL':'UI-Venus-2-9B'}
        summaries=[];remove=batch.shutil.rmtree
        def collect(path):
            summaries.append(json.loads((path/'summary.json').read_text()))
            batch.sys.stdout.streams[-1].close();remove(path)
        with tempfile.TemporaryDirectory() as directory,patch.object(batch,'_read_env',return_value=values),patch.object(batch,'_select_tasks',return_value=[(1,2001)]),patch.object(batch,'_task_metadata',return_value={}),patch.object(batch,'_run_child',side_effect=AssertionError('Forbidden subprocess')),patch.object(batch.shutil,'rmtree',side_effect=collect),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(batch.main(['--tasks','1','--dry-run','--bench-root',str(self.root),'--output-root',directory,
                '--configuration-source','explicit_executor_spec','--completion-policy','native','--condition-updates','disabled']),0)
        self.assertEqual(summaries[0]['executor']['agent'],'ui_venus')
        self.assertEqual(summaries[0]['executor']['max_output_tokens'],16384)
        self.assertEqual(summaries[0]['executor']['condition_updates_protocol'],'unsupported')
