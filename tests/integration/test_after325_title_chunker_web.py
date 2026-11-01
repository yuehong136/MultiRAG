"""Pinned title-chunker serializer and API client persist exact rules in SQL."""

import json
import os
import subprocess
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import UserCanvas
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout

CLIENT = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const root=process.env.WEB_COMPLETION_CHECKOUT, base=process.env.TITLE_API_BASE;
let token=process.env.TITLE_API_TOKEN;
Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:key=>key==='auth_token'?token:null,setItem:(_key,value)=>{token=value},removeItem:()=>{token=null}}});
const {apiClient}=await import(pathToFileURL(root+'/src/api/client.ts'));
const {agentAPI}=await import(pathToFileURL(root+'/src/api/agent.ts'));
const {deserializeDslToGraph,serializeGraphToDsl}=await import(pathToFileURL(root+'/src/pages/agent/operators/serializers.ts'));
apiClient.setAuthToken(token);
const fetchHTTP=globalThis.fetch, paths=[];
globalThis.fetch=(input,init)=>{const url=new URL(String(input),base);assert.ok(url.pathname.startsWith('/api/v1/agents'));paths.push(url.pathname);return fetchHTTP(base+url.pathname+url.search,init)};
const id='TitleChunker:contract', mode=process.env.TITLE_MODE;
const legacy={components:{[id]:{obj:{component_name:'TitleChunker',params:{method:'hierarchy',hierarchy:2,levels:[['^Part','^Chapter']]}},upstream:[],downstream:[]}},history:[],path:[]};
const created=await agentAPI.setAgent({title:'Title rules '+mode,canvas_type:'pipeline',canvas_category:'dataflow_canvas',dsl:legacy});
assert.ok(created.data?.id);
const graph=deserializeDslToGraph(legacy), form=graph.nodes.find(node=>node.id===id).data.form;
assert.equal(form.hierarchyRules.length,1);
form.hierarchyRules[0].levels[0].expression='^PartEdited';
form.groupRules=[{levels:[{expression:'^GroupOnly'}]}];
form.method=mode;
const dsl=serializeGraphToDsl({graph,baseDsl:legacy});
const expected=mode==='group'?[['^GroupOnly']]:[['^PartEdited','^Chapter']];
assert.deepEqual(dsl.components[id].obj.params.levels,expected);
await agentAPI.setAgent({id:created.data.id,dsl});
const loaded=await agentAPI.fetchAgent(created.data.id);
const readDsl=typeof loaded.data.dsl==='string'?JSON.parse(loaded.data.dsl):loaded.data.dsl;
const again=serializeGraphToDsl({graph:deserializeDslToGraph(readDsl),baseDsl:readDsl});
assert.deepEqual(again.components[id].obj.params.levels,expected);
assert.ok(paths.length===3);
console.log(JSON.stringify({id:created.data.id,node:id,mode,levels:expected,paths}));
"""


@pytest.mark.external_consumer
@pytest.mark.parametrize("mode", ["hierarchy", "group"])
def test_pinned_title_rules_survive_actual_client_http_and_sql(release_api: dict[str, Any], mode: str) -> None:
    env = release_api
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("title-chunker-web-client-sql")
    journal.event("reserve-consumer-execution", endpoint=env["base"], mode=mode, web_sha=web_sha)
    result = subprocess.run(
        ["node", "--import", str(checkout / "node_modules/tsx/dist/esm/index.mjs"), "--input-type=module", "--eval", CLIENT],
        cwd=checkout,
        env={**os.environ, "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json"), "TITLE_API_BASE": env["base"], "TITLE_API_TOKEN": env["jwts"][0], "TITLE_MODE": mode},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    with Session(env["engine"]) as reader:
        row = reader.scalar(select(UserCanvas).where(UserCanvas.id == receipt["id"]))
        assert row is not None and row.user_id == env["owners"][0] and row.canvas_category == "dataflow_canvas"
        stored = json.loads(row.dsl) if isinstance(row.dsl, str) else row.dsl
        params = stored["components"][receipt["node"]]["obj"]["params"]
        assert params["method"] == mode and params["levels"] == receipt["levels"]
        assert "hierarchyRules" not in params and "groupRules" not in params
    journal.event("web-title-independent-sql-readback", canvas=receipt["id"], mode=mode, exact_rules=True, active_mode_only=True, web_sha=web_sha)
