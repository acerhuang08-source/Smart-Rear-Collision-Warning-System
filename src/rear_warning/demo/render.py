"""Render a self-contained offline HTML player for precomputed demo data."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智慧型後方接近警示系統｜融合判斷展示</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#102238;--panel2:#0b1a2c;--ink:#f4f8ff;--muted:#9fb4ca;--cyan:#27d7ff;--green:#47e89b;--yellow:#ffd166;--red:#ff637d;--purple:#b894ff;--line:#24415f}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#173d60 0,transparent 38%),var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Noto Sans TC","Microsoft JhengHei",sans-serif;min-height:100vh}
.shell{width:min(100vw,177.78vh);height:min(100vh,56.25vw);margin:auto;padding:clamp(10px,1.2vw,20px);display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;gap:clamp(8px,.8vw,13px);overflow:hidden}
header{display:flex;justify-content:space-between;align-items:center;gap:20px}.eyebrow{font-size:clamp(12px,1vw,17px);letter-spacing:.12em;color:var(--cyan);font-weight:800}.source{flex:none;border:1px solid var(--cyan);background:#06334a;color:#a9f1ff;border-radius:999px;padding:.55em 1em;font-weight:800;font-size:clamp(12px,1.05vw,18px)}
h1{font-size:clamp(23px,2vw,36px);line-height:1.1;margin:.12em 0}.scenario-line{display:flex;align-items:baseline;gap:.8em;flex-wrap:wrap}.scenario-line strong{font-size:clamp(19px,1.45vw,26px)}.phase{color:var(--yellow);font-size:clamp(13px,1vw,17px)}
.grid{display:grid;grid-template-columns:1.06fr .94fr;gap:clamp(10px,1vw,18px);min-height:0}.column{display:grid;gap:clamp(10px,1vw,18px);min-height:0}.left{grid-template-rows:auto 1fr}.right{grid-template-rows:1fr 1fr}
.card{background:linear-gradient(145deg,rgba(19,43,69,.96),rgba(8,26,44,.96));border:1px solid var(--line);border-radius:18px;padding:clamp(12px,1.05vw,18px);box-shadow:0 16px 50px #0005;overflow:hidden}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:10px}.metric{background:#081a2c;border:1px solid #1f4262;border-radius:14px;padding:11px 14px}.label{color:var(--muted);font-size:clamp(12px,.88vw,15px);margin-bottom:4px}.value{font-variant-numeric:tabular-nums;font-weight:850;font-size:clamp(27px,2.5vw,45px);line-height:1.02}.unit{font-size:.42em;color:var(--muted);margin-left:.2em}.sub{font-size:clamp(11px,.78vw,13px);color:var(--muted);margin-top:5px}
.chart-card{display:flex;flex-direction:column}.chart-head{display:flex;justify-content:space-between;align-items:center}.chart-head h2,.card h2{font-size:clamp(15px,1.15vw,21px);margin:0 0 10px}.legend{font-size:12px;color:var(--muted)}canvas{width:100%;height:100%;min-height:150px;display:block}
.classification{border-left:6px solid var(--purple)}.class-main{font-size:clamp(22px,1.75vw,32px);font-weight:900;line-height:1.15;margin:.16em 0}.enum{display:inline-block;color:#d8caff;background:#2f2051;border-radius:7px;padding:.2em .5em;font-family:ui-monospace,monospace;font-size:clamp(11px,.75vw,13px)}.reason{margin-top:8px;color:#dce8f5;font-size:clamp(13px,.92vw,16px);line-height:1.35}.candidate{color:var(--yellow);margin-top:5px;min-height:1.25em}
.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.evidence{border:1px solid var(--line);background:#0a1c2e;border-radius:12px;padding:8px 10px}.evidence b{display:block;font-size:clamp(14px,.98vw,17px);margin-top:2px}.yes{color:var(--yellow)}.no{color:var(--green)}.notready{color:var(--muted)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:7px}.status-box{border-radius:12px;padding:8px 10px;background:#081a2c;border:1px solid var(--line)}.warning{border-left:6px solid var(--green)}.warning-value{font-size:clamp(20px,1.4vw,26px);font-weight:900}.warning-note{color:var(--yellow);font-size:clamp(11px,.75vw,13px);margin-top:5px}
.controls{background:#091b2e;border:1px solid var(--line);border-radius:16px;padding:9px 13px;display:grid;grid-template-columns:auto minmax(220px,1fr) auto auto;gap:10px;align-items:center}button,select{border:1px solid #34628b;background:#123554;color:var(--ink);border-radius:9px;padding:.5em .72em;font:inherit;font-weight:750;cursor:pointer}button:hover,select:hover{border-color:var(--cyan)}button:focus-visible,select:focus-visible,input:focus-visible{outline:3px solid #27d7ff88;outline-offset:2px}.buttons{display:flex;gap:7px}.timeline{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center}input[type=range]{width:100%;accent-color:var(--cyan)}.time{font-variant-numeric:tabular-nums;min-width:110px;text-align:right}.selectors{display:flex;gap:8px;align-items:center}.meta{font-size:11px;color:#7891a9;text-align:right}.fault{color:var(--red)!important}
@media(max-width:900px){.shell{width:100%;height:auto;min-height:100vh;overflow:visible}.grid{grid-template-columns:1fr}.controls{grid-template-columns:1fr}.meta{text-align:left}.right{grid-template-rows:auto}.value{font-size:34px}}
</style>
</head>
<body>
<main class="shell">
  <header>
    <div><div class="eyebrow">SMART REAR COLLISION WARNING</div><h1>後方距離 × 自身動作 × 相對運動分類</h1></div>
    <div class="source" id="sourceLabel">合成資料模擬</div>
  </header>
  <div class="scenario-line"><strong id="scenarioName"></strong><span class="phase" id="phase"></span></div>
  <section class="grid">
    <div class="column left">
      <div class="metrics">
        <article class="metric"><div class="label">後方距離</div><div class="value" id="distance">—</div><div class="sub" id="distanceStatus"></div></article>
        <article class="metric"><div class="label">沿感測方向的接近速率</div><div class="value" id="closingRate">資料不足</div><div class="sub">僅代表距離變化，不是目標絕對車速</div></article>
      </div>
      <article class="card chart-card"><div class="chart-head"><h2>距離隨時間變化</h2><span class="legend">青線：有效距離　紅點：無效輸入</span></div><canvas id="chart" aria-label="距離時間曲線"></canvas></article>
    </div>
    <div class="column right">
      <article class="card classification">
        <h2>相對運動分類（既有融合演算法輸出）</h2>
        <div class="class-main" id="classification"></div><span class="enum" id="classificationEnum"></span>
        <div class="candidate" id="candidate"></div><div class="reason" id="reason"></div>
        <div class="split"><div class="status-box"><div class="label">近期靜止基準</div><strong id="baseline"></strong></div><div class="status-box"><div class="label">資料品質／時間關聯</div><strong id="sync"></strong></div></div>
      </article>
      <article class="card warning">
        <h2>自身動作證據</h2>
        <div class="evidence-grid"><div class="evidence"><span class="label">平移證據</span><b id="translation"></b></div><div class="evidence"><span class="label">旋轉證據</span><b id="rotation"></b></div></div>
        <div class="split"><div class="status-box"><div class="label">證據視窗</div><strong id="motionReady"></strong></div><div class="status-box"><div class="label">距離趨勢</div><strong id="trendReady"></strong></div></div>
        <hr style="border:0;border-top:1px solid var(--line);margin:13px 0">
        <div class="label">距離警示模擬（獨立於融合分類）</div><div class="warning-value" id="warning"></div><div class="enum" id="warningEnum"></div><div class="warning-note">只呼叫 WarningPolicy／stabilizer；不控制 GPIO 或實體 LED。</div>
      </article>
    </div>
  </section>
  <section class="controls" aria-label="播放控制">
    <div class="buttons"><button id="play">▶ 播放</button><button id="restart">↺ 重播</button></div>
    <div class="timeline"><input id="progress" type="range" min="0" value="0" step="1"><span class="time" id="time"></span></div>
    <div class="selectors"><label for="scenario">情境</label><select id="scenario"></select><label for="speed">速度</label><select id="speed"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option><option value="4">4×</option></select></div>
    <div class="meta" id="meta"></div>
  </section>
</main>
<script id="demoData" type="application/json">__DEMO_DATA__</script>
<script>
"use strict";
const bundle=JSON.parse(document.getElementById("demoData").textContent);
const $=id=>document.getElementById(id);let scenarioIndex=0,frameIndex=0,timer=null;
const scenarioSelect=$("scenario"),speedSelect=$("speed"),progress=$("progress");
bundle.scenarios.forEach((item,index)=>{const option=document.createElement("option");option.value=String(index);option.textContent=`${item.id}｜${item.name}`;scenarioSelect.append(option)});
function currentScenario(){return bundle.scenarios[scenarioIndex]}function currentFrame(){return currentScenario().frames[frameIndex]}
function evidenceText(ready,detected){if(!ready)return["資料不足","notready"];return detected?["達判定條件","yes"]:["未達判定條件","no"]}
function setEvidence(id,ready,detected){const node=$(id),result=evidenceText(ready,detected);node.textContent=result[0];node.className=result[1]}
function drawChart(){const canvas=$("chart"),box=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;canvas.width=Math.max(1,Math.round(box.width*ratio));canvas.height=Math.max(1,Math.round(box.height*ratio));const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);const w=box.width,h=box.height,p={l:46,r:15,t:14,b:30},frames=currentScenario().frames,values=frames.map(f=>f.output.distance_m).filter(v=>v!==null);const min=Math.min(...values)-.15,max=Math.max(...values)+.15,x=i=>p.l+i/(frames.length-1)*(w-p.l-p.r),y=v=>p.t+(max-v)/(max-min)*(h-p.t-p.b);ctx.clearRect(0,0,w,h);ctx.strokeStyle="#24415f";ctx.fillStyle="#91a9c0";ctx.font="12px system-ui";for(let i=0;i<4;i++){const value=min+(max-min)*i/3,py=y(value);ctx.beginPath();ctx.moveTo(p.l,py);ctx.lineTo(w-p.r,py);ctx.stroke();ctx.fillText(value.toFixed(1)+" m",3,py+4)}ctx.beginPath();ctx.strokeStyle="#27d7ff";ctx.lineWidth=3;let pen=false;frames.slice(0,frameIndex+1).forEach((f,i)=>{const value=f.output.distance_m;if(value===null){pen=false;ctx.fillStyle="#ff637d";ctx.beginPath();ctx.arc(x(i),h/2,5,0,Math.PI*2);ctx.fill();return}if(!pen){ctx.moveTo(x(i),y(value));pen=true}else ctx.lineTo(x(i),y(value))});ctx.stroke();ctx.fillStyle="#91a9c0";ctx.fillText("0 s",p.l,h-8);ctx.fillText(frames.at(-1).timestamp_seconds.toFixed(1)+" s",w-p.r-38,h-8)}
function render(){const scenario=currentScenario(),frame=currentFrame(),input=frame.input,output=frame.output;$("sourceLabel").textContent=scenario.source_label;$("scenarioName").textContent=`情境 ${scenario.id}｜${scenario.name}`;$("phase").textContent=`目前階段：${frame.phase}`;if(output.distance_m===null){$("distance").textContent="資料無效";$("distance").className="value fault"}else{$("distance").innerHTML=`${output.distance_m.toFixed(2)}<span class="unit">公尺</span>`;$("distance").className="value"}$("distanceStatus").textContent=input.sync_status_zh_tw;if(output.closing_rate_mps===null){$("closingRate").textContent="資料不足"}else{$("closingRate").innerHTML=`${output.closing_rate_mps.toFixed(2)}<span class="unit">公尺／秒</span>`}$("classification").textContent=output.classification_zh_tw;$("classificationEnum").textContent=output.classification.toUpperCase();$("candidate").textContent=output.candidate_classification?`候選：${output.candidate_classification_zh_tw}（${output.candidate_classification.toUpperCase()}）`:"";$("reason").textContent=output.assessment_reason_zh_tw;$("baseline").textContent=output.stationary_baseline_ready?"基準足夠":"基準不足／已清除";$("baseline").className=output.stationary_baseline_ready?"no":"notready";const effectiveSync=output.assessment_reason==="motion_stale"?"動作資料已過期":input.sync_status_zh_tw;$("sync").textContent=effectiveSync;$("sync").className=(input.sync_status==="matched"&&output.assessment_reason!=="motion_stale")?"no":"fault";setEvidence("translation",output.own_motion_ready,output.translation_detected);setEvidence("rotation",output.own_motion_ready,output.rotation_detected);$("motionReady").textContent=output.own_motion_ready?"可判讀":"資料不足";$("trendReady").textContent=output.distance_trend_ready?"可估計":"資料不足";$("warning").textContent=output.warning_state_zh_tw;$("warningEnum").textContent=output.warning_state.toUpperCase();progress.max=String(scenario.frames.length-1);progress.value=String(frameIndex);$("time").textContent=`${frame.timestamp_seconds.toFixed(2)} / ${scenario.summary.duration_seconds.toFixed(2)} 秒`;$("meta").textContent=`HEAD ${bundle.version.git_head.slice(0,8)} · dirty=${bundle.version.git_dirty} · 樣本 ${frameIndex+1}/${scenario.frames.length}`;drawChart()}
function pause(){if(timer!==null){clearTimeout(timer);timer=null}$("play").textContent="▶ 播放"}
function schedule(){pause();$("play").textContent="❚❚ 暫停";const scenario=currentScenario();if(frameIndex>=scenario.frames.length-1)frameIndex=0;const advance=()=>{if(frameIndex>=scenario.frames.length-1){pause();return}frameIndex+=1;render();timer=setTimeout(advance,scenario.sample_interval_seconds*1000/Number(speedSelect.value))};timer=setTimeout(advance,scenario.sample_interval_seconds*1000/Number(speedSelect.value))}
$("play").addEventListener("click",()=>timer===null?schedule():pause());$("restart").addEventListener("click",()=>{const playing=timer!==null;pause();frameIndex=0;render();if(playing)schedule()});progress.addEventListener("input",()=>{frameIndex=Number(progress.value);render()});scenarioSelect.addEventListener("change",()=>{pause();scenarioIndex=Number(scenarioSelect.value);frameIndex=0;render()});speedSelect.addEventListener("change",()=>{if(timer!==null)schedule()});window.addEventListener("resize",drawChart);render();
function runBrowserSelfTest(){try{const timestamps=JSON.stringify(bundle.scenarios.map(s=>s.frames.map(f=>f.timestamp_seconds)));progress.value="10";progress.dispatchEvent(new Event("input"));if(frameIndex!==10)throw new Error("progress");$("restart").click();if(frameIndex!==0)throw new Error("restart");scenarioSelect.value="1";scenarioSelect.dispatchEvent(new Event("change"));if(scenarioIndex!==1||frameIndex!==0)throw new Error("scenario");speedSelect.value="4";speedSelect.dispatchEvent(new Event("change"));if(JSON.stringify(bundle.scenarios.map(s=>s.frames.map(f=>f.timestamp_seconds)))!==timestamps)throw new Error("speed-mutated-data");$("play").click();if(timer===null)throw new Error("play");$("play").click();if(timer!==null)throw new Error("pause");document.body.dataset.demoSelfTest="passed"}catch(error){document.body.dataset.demoSelfTest=`failed:${error.message}`}}
if(new URLSearchParams(window.location.search).has("self-test"))runBrowserSelfTest();
</script>
</body>
</html>
'''


def render_demo_html(bundle: Mapping[str, Any]) -> str:
    """Return one standalone UTF-8 HTML document with strict JSON data."""
    serialized = json.dumps(
        bundle,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    # Prevent user-controlled metadata from terminating the JSON script element.
    serialized = serialized.replace("<", "\\u003c")
    return _HTML_TEMPLATE.replace("__DEMO_DATA__", serialized)


__all__ = ["render_demo_html"]
