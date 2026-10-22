"""Desktop helper receipts preserve approval and never resend uncertain input."""
import asyncio
import json
from pathlib import Path

from cryptography.fernet import Fernet

from agent_zero_cli.computer_use import ComputerUseManager
from agent_zero_cli.client import A0Client
from agent_zero_cli.computer_use_backend import ComputerUseBackendSpec, ComputerUseBackendSelection
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.host_browser_durable import BrowserJournal, fingerprint


def request(action, instance, operation, **kwargs):
    value = {"op_id": "transport", "context_id": "synthetic-desktop", "action": action, **kwargs}
    value["durable"] = {"schema": 1, "deployment": "d" * 64, "session_id": "a" * 64,
                        "operation_id": operation * 64, "instance_id": instance, "fingerprint": fingerprint(value)}
    return value


def manager(tmp_path, *, cut=False, trust="persistent"):
    helper = tmp_path / "helper.py"
    counter = tmp_path / "effects"
    helper.write_text('''import json,os,sys
from pathlib import Path
for line in sys.stdin:
 request=json.loads(line);action=request['action']
 if action=='click':
  path=Path(''' + repr(str(counter)) + ''');path.write_text(path.read_text()+'click\\n' if path.exists() else 'click\\n')
  if ''' + repr(cut) + ''':os._exit(0)
 result={'session_id':'approved-session','status':'active'}
 print(json.dumps({'request_id':request.get('request_id'),'ok':True,'result':result}),flush=True)
''')
    spec = ComputerUseBackendSpec(backend_id="synthetic", backend_family="synthetic", priority=1,
        detect=lambda: True, features=("pointer-injection",), interpreter_strategy="current_python",
        helper_target=str(helper), trust_mode_support=("persistent", "allow"))
    journal = BrowserJournal(tmp_path / "journal", Fernet.generate_key().decode())
    result = ComputerUseManager(CLIConfig(computer_use_enabled=True, computer_use_trust_mode=trust),
        persist_enabled=False, backend_selection=ComputerUseBackendSelection(spec, True, ""), durable_journal=journal)
    return result, journal, counter


def test_real_helper_io_requires_approval_then_deduplicates_completed_action_and_target_session(tmp_path):
    async def scenario():
        owner, journal, counter = manager(tmp_path)
        first = await owner.handle_op(request("click", journal.instance_id, "b", x=.2, y=.3))
        assert first["code"] == "COMPUTER_USE_SESSION_REQUIRED"
        assert not counter.exists() and not owner._sessions["synthetic-desktop"].process
        started = await owner.handle_op(request("start_session", journal.instance_id, "c"))
        assert started["ok"] and started["data"]["session_id"] == "approved-session"
        click = request("click", journal.instance_id, "e", session_id="approved-session", x=.2, y=.3)
        completed = await owner.handle_op(click)
        assert completed["ok"]
        assert (await owner.handle_op(click))["data"] == completed["data"]
        client = A0Client("http://synthetic.invalid")
        client.on_computer_use_op = owner.handle_op
        wire = await client._handle_computer_use_op(click)
        assert wire["result"] == completed["data"] and "data" not in wire
        wrong = await owner.handle_op(request("click", journal.instance_id, "f", session_id="another-session", x=.2, y=.3))
        assert wrong["code"] == "COMPUTER_USE_SESSION_MISMATCH"
        assert counter.read_text() == "click\n"
        status = await owner.handle_op(request("durable_status", journal.instance_id, "9", target_operation_id="e" * 64))
        assert status["data"]["response"]["data"] == completed["data"]
        owner.set_enabled(False)
        assert (await owner.handle_op(click))["code"] == "COMPUTER_USE_DURABLE_HELD"
        await owner.close()
    asyncio.run(scenario())


def test_helper_lost_response_retains_unknown_intent_without_auto_arming_or_resend(tmp_path):
    async def scenario():
        owner, journal, counter = manager(tmp_path, cut=True)
        assert (await owner.handle_op(request("start_session", journal.instance_id, "b")))["ok"]
        click = request("click", journal.instance_id, "c", session_id="approved-session", x=.2, y=.3)
        assert (await owner.handle_op(click))["code"] == "COMPUTER_USE_DURABLE_HELD"
        repeated = await owner.handle_op(click)
        assert repeated["data"]["durable"]["state"] == "outcome_unknown"
        assert counter.read_text() == "click\n"
        await owner.close()
    asyncio.run(scenario())


def test_durable_start_preserves_rearm_required_without_granting_trust(tmp_path):
    async def scenario():
        owner, journal, counter = manager(tmp_path, trust="allow")
        result = await owner.handle_op(request("start_session", journal.instance_id, "b"))
        assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
        assert owner.trust_mode == "allow" and not owner.restore_token
        assert not counter.exists() and not owner._sessions["synthetic-desktop"].process
        await owner.close()
    asyncio.run(scenario())
