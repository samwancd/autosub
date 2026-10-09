# -*- coding: utf-8 -*-
"""浏览器界面（整页 HTML/CSS/JS 内联在这里）。

单独放一个模块的原因：打包成 exe 时不需要额外搬运静态文件，
import 一下就有页面，PyInstaller 不会漏文件。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>autosub · 视频自动加字幕</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#ffffff; --line:#e3e6ea; --text:#1b1f24; --muted:#68707a;
    --brand:#2f6fed; --brand-weak:#eaf1ff; --ok:#0f8a4a; --ok-weak:#e8f7ee;
    --warn:#a86400; --warn-weak:#fff5e6; --err:#c0392b; --err-weak:#fdecea;
    --radius:14px; --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.06);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#14171a; --panel:#1c2024; --line:#2b3137; --text:#e8ebee; --muted:#9aa4af;
      --brand:#6a9dff; --brand-weak:#1d283a; --ok:#4ec98a; --ok-weak:#16291f;
      --warn:#e2b25c; --warn-weak:#2a2317; --err:#ef7a6d; --err-weak:#2c1c1a;
      --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
  header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
  h1{font-size:22px;margin:0;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:13px}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 22px}
  .chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;
    padding:4px 11px;font-size:12px;color:var(--muted)}
  .chip b{color:var(--text);font-weight:600}
  .chip.ok{border-color:var(--ok);color:var(--ok)}
  .chip.bad{border-color:var(--err);color:var(--err)}
  .chip.gpu{border-color:var(--brand);color:var(--brand)}
  .chip.cpu{border-color:var(--err);color:var(--err)}
  .chip.gpu b,.chip.cpu b{color:inherit}
  .ver{font-size:12px;font-weight:600;color:var(--brand);background:var(--brand-weak);
    border-radius:999px;padding:2px 9px;letter-spacing:.3px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    box-shadow:var(--shadow);padding:18px;margin-bottom:18px}
  .card h2{font-size:15px;margin:0 0 14px;display:flex;align-items:center;gap:8px}
  .card h2 .n{display:inline-flex;width:20px;height:20px;border-radius:50%;
    background:var(--brand-weak);color:var(--brand);font-size:12px;align-items:center;justify-content:center}
  .drop{border:1.5px dashed var(--line);border-radius:12px;padding:26px;text-align:center;
    cursor:pointer;transition:.15s}
  .drop:hover,.drop.hot{border-color:var(--brand);background:var(--brand-weak)}
  .drop strong{display:block;font-size:15px;margin-bottom:4px}
  .drop span{color:var(--muted);font-size:12.5px}
  .row{display:flex;gap:14px;flex-wrap:wrap}
  .field{display:flex;flex-direction:column;gap:6px;flex:1 1 170px;min-width:150px}
  .field label{font-size:12.5px;color:var(--muted)}
  select,input[type=text],input[type=number]{background:var(--bg);color:var(--text);
    border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:13.5px;width:100%}
  select:focus,input:focus{outline:2px solid var(--brand);outline-offset:-1px;border-color:transparent}
  .check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);margin-top:6px}
  .btn{border:0;border-radius:10px;padding:11px 22px;font-size:14px;font-weight:600;
    background:var(--brand);color:#fff;cursor:pointer}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line);font-weight:500}
  .btn.small{padding:7px 14px;font-size:13px}
  .bar{height:9px;background:var(--bg);border:1px solid var(--line);border-radius:999px;overflow:hidden}
  .bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--brand),#7aa8ff);
    transition:width .35s ease}
  .statusline{display:flex;justify-content:space-between;font-size:12.5px;color:var(--muted);margin:10px 0 8px}
  /* 预览框：按视频自身比例显示，只设上限（横屏约 500×280，竖屏约 158×280） */
  video{width:auto;height:auto;max-width:100%;max-height:280px;margin:0 auto;
    border-radius:10px;background:#000;display:block}
  .panes{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media (max-width:760px){.panes{grid-template-columns:1fr}}
  .pane .vlabel{margin-bottom:6px}
  .ph{border:1.5px dashed var(--line);border-radius:10px;min-height:180px;
    display:flex;align-items:center;justify-content:center;text-align:center;
    color:var(--muted);font-size:13px;background:var(--bg);padding:14px}
  /* 点击小预览 → 全屏放大播放 */
  .panes video{cursor:zoom-in}
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.78);display:none;
    align-items:center;justify-content:center;z-index:50;padding:24px;cursor:zoom-out}
  .lightbox.open{display:flex}
  .lightbox video{max-width:min(94vw,1000px);max-height:90vh;width:auto;height:auto;
    margin:0;border-radius:12px;background:#000;cursor:default;
    box-shadow:0 12px 48px rgba(0,0,0,.5)}
  .lightbox .x{position:absolute;top:10px;right:20px;font-size:30px;line-height:1;
    color:#fff;opacity:.75;cursor:pointer;user-select:none}
  .lightbox .x:hover{opacity:1}
  .zoomhint{font-size:11.5px;color:var(--muted)}
  .vwrap{margin-top:12px}
  .vlabel{font-size:12.5px;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between}
  .files{display:flex;gap:10px;flex-wrap:wrap;margin:4px 0 14px}
  .dl{display:flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:11px;
    padding:10px 14px;text-decoration:none;color:var(--text);background:var(--bg);flex:1 1 260px}
  .dl:hover{border-color:var(--brand)}
  .dl .ico{width:30px;height:30px;border-radius:8px;background:var(--brand-weak);color:var(--brand);
    display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
  .dl small{display:block;color:var(--muted);font-size:12px;font-weight:400}
  .dl b{font-size:13.5px}
  pre.log{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;
    font:12px/1.55 ui-monospace,Consolas,"Courier New",monospace;color:var(--muted);
    max-height:200px;overflow:auto;white-space:pre-wrap;margin:12px 0 0}
  .msg{border-radius:10px;padding:10px 13px;font-size:13px;margin-top:12px;display:none}
  .msg.err{display:block;background:var(--err-weak);color:var(--err)}
  .msg.ok{display:block;background:var(--ok-weak);color:var(--ok)}
  .msg.warn{display:block;background:var(--warn-weak);color:var(--warn)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:500;font-size:12px}
  td a{color:var(--brand);text-decoration:none}
  .muted{color:var(--muted)}
  .hide{display:none!important}
  .spread{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>autosub <span class="ver" id="ver">v—</span></h1>
    <span class="sub">本地 Whisper 转写 + ffmpeg 烧录 · 视频进，字幕文件和带字幕视频出</span>
  </header>
  <div class="chips" id="chips"><span class="chip">正在读取环境…</span></div>

  <!-- 1 选择视频 -->
  <div class="card">
    <h2><span class="n">1</span>选择原始视频</h2>
    <div class="drop" id="drop">
      <strong>把视频拖到这里，或点击选择</strong>
      <span>支持 mp4 / mov / mkv / avi / flv / webm …，文件只在本机处理</span>
    </div>
    <input type="file" id="fileInput" accept="video/*" class="hide">
    <div class="vwrap hide" id="pickedLine">
      <span class="muted">已选择：<span id="pickedName"></span></span>
    </div>
  </div>

  <!-- 2 参数 -->
  <div class="card">
    <h2><span class="n">2</span>参数</h2>
    <div class="row">
      <div class="field">
        <label>转写模型</label>
        <select id="optModel">
          <option value="auto">自动（有显卡 large-v3，否则 small）</option>
          <option value="tiny">tiny（最快，质量差）</option>
          <option value="base">base</option>
          <option value="small">small</option>
          <option value="medium">medium</option>
          <option value="large-v3">large-v3（最准，需显卡更佳）</option>
          <option value="large-v3-turbo">large-v3-turbo</option>
        </select>
      </div>
      <div class="field">
        <label>语言</label>
        <select id="optLang">
          <option value="">自动识别</option>
          <option value="zh">中文</option>
          <option value="en">英语</option>
          <option value="ja">日语</option>
          <option value="ko">韩语</option>
          <option value="yue">粤语</option>
        </select>
      </div>
      <div class="field">
        <label>输出方式</label>
        <select id="optMode">
          <option value="burn">烧录进画面（推荐，任何播放器都能看）</option>
          <option value="soft">软字幕轨（播放器可开关）</option>
          <option value="srt">只要字幕文件</option>
        </select>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="field">
        <label>每行最多字数</label>
        <input type="number" id="optChars" value="16" min="6" max="40">
      </div>
      <div class="field">
        <label>字号</label>
        <input type="number" id="optFontSize" value="20" min="8" max="72">
      </div>
      <div class="field">
        <label>距底部（像素）</label>
        <input type="number" id="optMargin" value="28" min="0" max="400">
      </div>
    </div>
    <div class="row" style="margin-top:10px">
      <div class="field" style="flex:2 1 420px">
        <label>本地模型目录（可选；填了就不用上面选的模型，也不联网）</label>
        <input type="text" id="optModelDir" placeholder="例：D:\models\faster-whisper-large-v3">
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="field" style="flex:3 1 380px">
        <label>专有名词提示（可选，用逗号分隔的地名/人名/术语，能明显提升识别率）</label>
        <input type="text" id="optPrompt" placeholder="例：兴义，赫章，韭菜坪，泸州">
      </div>
    </div>
    <label class="check"><input type="checkbox" id="optMirror"> 国内网络：用 hf-mirror.com 下载模型</label>
    <label class="check"><input type="checkbox" id="optOffline"> 离线模式：只用本地已下载的权重，绝不联网</label>
    <div class="spread" style="margin-top:16px">
      <button class="btn" id="btnStart" disabled>开始生成字幕</button>
      <span class="muted" id="hint">先选一个视频</span>
    </div>
    <div class="statusline"><span id="stage">待机</span><span id="pct">0%</span></div>
    <div class="bar"><i id="bar"></i></div>
    <pre class="log hide" id="log"></pre>
    <div class="msg" id="msg"></div>
  </div>

  <!-- 3 结果 -->
  <div class="card" id="resultCard">
    <h2><span class="n">3</span>结果</h2>
    <div class="files" id="fileLinks"><span class="muted">还没有生成结果</span></div>
    <div class="panes">
      <div class="pane">
        <div class="vlabel"><span>原始视频 <span class="zoomhint">（点击放大播放）</span></span><span class="muted" id="srcMeta"></span></div>
        <div class="ph" id="srcEmpty">选好视频后，这里显示原始视频</div>
        <div class="hide" id="srcBox"><video id="srcVideo" controls playsinline preload="metadata"></video></div>
      </div>
      <div class="pane">
        <div class="vlabel"><span>带字幕视频（最后生成的那一个） <span class="zoomhint">（点击放大播放）</span></span><span class="muted" id="outMeta"></span></div>
        <div class="ph" id="outEmpty">开始生成后，这里显示带字幕的视频</div>
        <div class="hide" id="outBox"><video id="outVideo" controls playsinline preload="metadata"></video></div>
      </div>
    </div>
  </div>

  <!-- 4 历史 -->
  <div class="card">
    <h2><span class="n">4</span>本次会话的任务</h2>
    <table>
      <thead><tr><th>视频</th><th>状态</th><th>模型</th><th>结果</th><th>时间</th><th>耗时</th></tr></thead>
      <tbody id="jobRows"><tr><td colspan="6" class="muted">暂无</td></tr></tbody>
    </table>
  </div>

  <!-- 点击放大后的全屏播放层 -->
  <div class="lightbox" id="lb">
    <span class="x" id="lbClose" title="关闭（Esc）">×</span>
    <video id="lbVideo" controls playsinline preload="metadata"></video>
  </div>

  <p class="muted" style="font-size:12px">
    视频只保存在本机（用户目录下的 .autosub/jobs），不会上传到任何服务器；
    转写在本地完成，只有第一次下载模型权重时需要联网。
  </p>
</div>

<script>
(function(){
  const $ = (id) => document.getElementById(id);
  const state = { file:null, job:null, timer:null, busy:false };
  const STATE_TEXT = { new:"待上传", uploading:"上传中", queued:"排队中",
                       running:"处理中", done:"完成", error:"出错" };

  function fmtSize(n){
    if(n === null || n === undefined) return "—";
    const u = ["B","KB","MB","GB"]; let x = Number(n), i = 0;
    while(x >= 1024 && i < u.length-1){ x /= 1024; i++; }
    return (i === 0 ? x : x.toFixed(1)) + u[i];
  }
  function fmtTime(ts){
    const d = new Date(ts*1000);
    const p = (n)=>String(n).padStart(2,"0");
    return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
  }
  function setMsg(kind, text){
    const el = $("msg");
    el.className = "msg" + (kind ? " "+kind : "");
    el.textContent = text || "";
  }
  function setProgress(frac, stage){
    $("bar").style.width = Math.round(frac*100) + "%";
    $("pct").textContent = Math.round(frac*100) + "%";
    if(stage) $("stage").textContent = stage;
  }

  // ---------- 环境信息 ----------
  fetch("/api/doctor").then(r=>r.json()).then(d=>{
    if(d.version) $("ver").textContent = "v" + d.version;
    const chips = [];
    const chip = (label,val,cls)=>chips.push(
      '<span class="chip '+(cls||'')+'">'+label+ ' <b>'+val+'</b></span>');
    // GPU 蓝、CPU 红，一眼分辨
    const isGpu = d.device === "cuda";
    chip("设备", isGpu ? ("GPU" + (d.cuda > 1 ? " ×" + d.cuda : "")) : "CPU", isGpu ? "gpu" : "cpu");
    chip("默认模型", d.model);
    chip("ffmpeg", d.ffmpeg ? (d.ffmpeg_tag || "已找到") : "未找到", d.ffmpeg ? "ok" : "bad");
    chip("烧录能力", d.can_burn ? "可用" : "不可用", d.can_burn ? "ok" : "bad");
    chip("中文字体", d.font ? d.font : "未找到", d.font ? "ok" : "bad");
    if(d.ready === false) chip("依赖", "缺 faster-whisper", "bad");
    $("chips").innerHTML = chips.join("");
  }).catch(()=>{ $("chips").innerHTML = '<span class="chip bad">无法读取环境信息</span>'; });

  // ---------- 选文件 ----------
  const drop = $("drop"), fileInput = $("fileInput");
  drop.addEventListener("click", ()=>fileInput.click());
  drop.addEventListener("dragover", e=>{ e.preventDefault(); drop.classList.add("hot"); });
  drop.addEventListener("dragleave", ()=>drop.classList.remove("hot"));
  drop.addEventListener("drop", e=>{
    e.preventDefault(); drop.classList.remove("hot");
    if(e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", ()=>{ if(fileInput.files.length) selectFile(fileInput.files[0]); });

  let objUrl = null;
  function selectFile(f){
    if(!/\.(mp4|mov|mkv|avi|flv|webm|m4v|ts|mpg|mpeg|wmv)$/i.test(f.name)){
      setMsg("warn", "这个文件看起来不是视频：" + f.name); return;
    }
    state.file = f;
    if(objUrl) URL.revokeObjectURL(objUrl);
    objUrl = URL.createObjectURL(f);
    // 左侧：原始视频
    $("srcVideo").src = objUrl;
    $("srcEmpty").classList.add("hide");
    $("srcBox").classList.remove("hide");
    $("srcMeta").textContent = f.name + " · " + fmtSize(f.size);
    $("pickedLine").classList.remove("hide");
    $("pickedName").textContent = f.name + " · " + fmtSize(f.size);
    // 右侧：重置上一次的结果
    $("outVideo").removeAttribute("src");
    $("outMeta").textContent = "";
    $("outBox").classList.add("hide");
    $("outEmpty").classList.remove("hide");
    $("outEmpty").textContent = "开始生成后，这里显示带字幕的视频";
    $("btnStart").disabled = false;
    $("hint").textContent = "准备好后点开始";
    setMsg("", "");
    setProgress(0, "待机");
  }

  function opts(){
    return {
      name: state.file.name,
      mode: $("optMode").value,
      model: ($("optModelDir").value.trim() || $("optModel").value),
      language: $("optLang").value || null,
      initial_prompt: $("optPrompt").value.trim() || null,
      max_chars: parseInt($("optChars").value,10) || 16,
      font_size: parseInt($("optFontSize").value,10) || 20,
      margin_v: parseInt($("optMargin").value,10) || 28,
      hf_mirror: $("optMirror").checked,
      offline: $("optOffline").checked
    };
  }

  // ---------- 开始 ----------
  $("btnStart").addEventListener("click", async ()=>{
    if(state.busy || !state.file) return;
    state.busy = true;
    $("btnStart").disabled = true;
    $("log").classList.remove("hide");
    $("log").textContent = "";
    setMsg("", "");
    setProgress(0, "创建任务…");

    let job;
    try{
      const r = await fetch("/api/jobs", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(opts())});
      job = await r.json();
      if(!r.ok) throw new Error(job.error || "创建任务失败");
    }catch(e){
      finishError("创建任务失败：" + e.message); return;
    }
    state.job = job.id;

    setProgress(0.01, "上传中…");
    upload(job.id, state.file, (sent, total)=>{
      setProgress(0.01 + 0.14*(sent/Math.max(total,1)), "上传中… " + Math.round(sent/total*100) + "%");
    }, (ok, err)=>{
      if(!ok){ finishError("上传失败：" + err); return; }
      setProgress(0.16, "排队…");
      fetch("/api/jobs/"+job.id+"/start", {method:"POST"}).then(r=>r.json())
        .then(()=>{ poll(); })
        .catch(e=>finishError("启动失败：" + e.message));
    });
  });

  function upload(id, file, onProgress, done){
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/jobs/" + id + "/file?name=" + encodeURIComponent(file.name), true);
    xhr.upload.onprogress = (e)=>{ if(e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = ()=>{
      if(xhr.status >= 200 && xhr.status < 300) done(true);
      else done(false, (xhr.responseText || ("HTTP " + xhr.status)).slice(0,300));
    };
    xhr.onerror = ()=>done(false, "网络中断");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.send(file);
  }

  function poll(){
    clearTimeout(state.timer);
    if(!state.job) return;
    fetch("/api/jobs/" + state.job).then(r=>r.json()).then(job=>{
      if(job.error){ finishError(job.error); return; }
      // 全局进度：上传占 0~0.16，其余映射到 0.16~1
      const p = 0.16 + 0.84 * (job.progress || 0);
      setProgress(p, (STATE_TEXT[job.state] || job.state) + (job.stage ? " · " + job.stage : ""));
      if(job.log && job.log.length) $("log").textContent = job.log.join("\n");
      $("log").scrollTop = $("log").scrollHeight;

      if(job.state === "done" || job.state === "error"){
        state.busy = false;
        $("btnStart").disabled = false;
        renderResult(job);
        refreshJobs();
        return;
      }
      state.timer = setTimeout(poll, 800);
    }).catch(()=>{ state.timer = setTimeout(poll, 1500); });
  }

  function finishError(text){
    state.busy = false;
    $("btnStart").disabled = false;
    setMsg("err", text);
    $("hint").textContent = "可以调整参数后重试";
    refreshJobs();
  }

  function renderResult(job){
    if(job.state === "error"){
      setMsg("err", job.message || "处理失败");
      $("hint").textContent = "可看下方日志定位原因";
      return;
    }
    const links = [];
    if(job.has_srt){
      links.push('<a class="dl" href="/api/jobs/'+job.id+'/download?what=srt">'
        + '<span class="ico">SRT</span><span><b>字幕文件</b><small>'+ (job.srt_name||"") +'</small></span></a>');
    }
    if(job.has_video){
      links.push('<a class="dl" href="/api/jobs/'+job.id+'/download?what=video">'
        + '<span class="ico">MP4</span><span><b>带字幕的视频</b><small>'+ (job.video_name||"") +'</small></span></a>');
    }
    $("fileLinks").innerHTML = links.length ? links.join("") : '<span class="muted">没有产出文件</span>';

    if(job.has_video){
      $("outVideo").src = "/api/jobs/"+job.id+"/media?what=video&t=" + Date.now();
      $("outEmpty").classList.add("hide");
      $("outBox").classList.remove("hide");
      $("outMeta").textContent = (job.video_name||"") + " · " + fmtSize(job.video_size);
    }else{
      $("outBox").classList.add("hide");
      $("outEmpty").classList.remove("hide");
      $("outEmpty").textContent = job.state === "error" ? "本次没有生成视频（看上方日志）" : "还没有生成视频";
      $("outMeta").textContent = "";
    }
    const kind = job.result_ok ? "ok" : "warn";
    setMsg(kind, (job.message || "完成") +
      (job.cues !== undefined ? "   ｜  共 " + job.cues + " 条字幕，用时 " + job.seconds + "s" : ""));
    $("hint").textContent = job.result_ok ? "完成，可以下载或预览" : "只产出了部分结果";
  }

  function refreshJobs(){
    fetch("/api/jobs").then(r=>r.json()).then(list=>{
      if(!list.length){ $("jobRows").innerHTML = '<tr><td colspan="6" class="muted">暂无</td></tr>'; return; }
      $("jobRows").innerHTML = list.map(j=>{
        const res = [];
        if(j.has_srt)  res.push('<a href="/api/jobs/'+j.id+'/download?what=srt">字幕</a>');
        if(j.has_video) res.push('<a href="/api/jobs/'+j.id+'/media?what=video" target="_blank">视频</a>');
        const el = (j.elapsed === null || j.elapsed === undefined) ? "" :
                   (j.elapsed + "s" + (j.state === "running" ? "…" : ""));
        return '<tr><td>'+j.name+'</td><td>'+(STATE_TEXT[j.state]||j.state)+'</td>'
          + '<td>'+(j.model ? j.model : '<span class="muted">—</span>')+'</td>'
          + '<td>'+(res.join(" · ")||'<span class="muted">—</span>')+'</td>'
          + '<td class="muted">'+fmtTime(j.created)+'</td>'
          + '<td class="muted">'+(el || '—')+'</td></tr>';
      }).join("");
    }).catch(()=>{});
  }

  // ---------- 点击预览 → 全屏放大播放 ----------
  const lb = $("lb"), lbVideo = $("lbVideo");
  function openLb(srcEl){
    if(!srcEl || !srcEl.src || srcEl.src === location.href) return;   // 还没有视频源
    lbVideo.src = srcEl.src;
    lb.classList.add("open");
    srcEl.pause();
    lbVideo.play().catch(()=>{});
  }
  function closeLb(){
    lb.classList.remove("open");
    lbVideo.pause();
  }
  ["srcVideo","outVideo"].forEach(id=>{
    $(id).addEventListener("click", (e)=>{ e.preventDefault(); openLb($(id)); });
  });
  lb.addEventListener("click", (e)=>{ if(e.target !== lbVideo) closeLb(); });
  $("lbClose").addEventListener("click", closeLb);
  document.addEventListener("keydown", (e)=>{ if(e.key === "Escape") closeLb(); });

  refreshJobs();
})();
</script>
</body>
</html>
"""
