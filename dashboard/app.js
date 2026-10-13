'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let csrf = '', status = null, currentPage = 'overview', selections = new Set(), courseNames = new Map(), coursesLoaded = false, dirtyCourses = false, scheduleDirty = false, localLectures = [], mailMessages = [], refreshing = false, toastTimer, courseRequest = 0;
const labels = {overview:'运行总览',courses:'课程管理',library:'讲义资料库',mail:'邮箱导入',settings:'设置'};
function toast(message, error=false) { $('toast').textContent=message; $('toast').className='show'+(error?' error':''); clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').className='',5000); }
async function api(path, body) {
  const options = body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':csrf},body:JSON.stringify(body)};
  const response = await fetch('/api'+path,options);
  const result = await response.json();
  if(!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function dateTime(value) { if(!value) return '—'; return new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}); }
async function action(button, path, body={}) {
  button.disabled=true;
  try { const result=await api(path,body); toast(result.message); await refresh(); }
  catch(error) { toast(error.message,true); }
  finally { button.disabled=false; if(status) renderStatus(status); }
}
function renderStatus(s) {
  const n=s.counters, completed=n.summaries, pending=Math.max(0,n.total-completed);
  $('connection').textContent='本机已连接'; $('connection').className='pill';
  $('clock').textContent=dateTime(s.now);
  $('run-title').textContent=s.running?'课程正在整理中。':'准备好继续学习了吗？';
  $('run-description').textContent=s.running?'语音识别、课件整理与讲义生成正在进行。已保存的结果会逐步出现在资料库。':'读取本地配置，检查你订阅的课程并整理课堂笔记。';
  $('start').disabled=s.running;
  $('stop').disabled=!s.running;
  $('run-detail').textContent=s.running?`正在运行 · PID ${s.processes.map(p=>p.pid).join(' / ')} · 开始于 ${dateTime(s.processes[0]?.started_at)}`:'当前没有课程处理进程 · 定时与邮箱导入可以单独配置';
  $('stat-summaries').textContent=completed; $('stat-pending').textContent=pending;
  $('stat-emailed').textContent=n.emailed; $('stat-failed').textContent=n.failed;
  $('process-state').textContent=s.running?'运行中':'未运行';
  if(s.running && /\[Qwen\] Runtime: cuda/.test(s.logs||'')) $('process-state').textContent='运行中 · GPU 加速';
  $('progress-count').textContent=`${completed} / ${n.total}`;
  $('progress-bar').style.width=(n.total?completed/n.total*100:0)+'%';
  $('memory').textContent=s.running?(s.processes.reduce((sum,p)=>sum+p.memory_mb,0)/1024).toFixed(1)+' GB':'—';
  $('transcripts').textContent=n.transcripts+' 个';
  $('blocks').textContent=s.blocks?`当前音频识别 ${s.blocks.done} / ${s.blocks.total} 块 · 此比例仅代表当前识别任务`:'语音块进度会在本站启动的任务日志中自动识别。';
  const completion=s.completion||{}; $('completion-notice').hidden=!completion.enabled; $('completion-message').textContent=(completion.message||'')+(completion.countdown_at?' · 剩余 '+Math.max(0,Math.ceil(completion.countdown_at-Date.now()/1000))+' 秒':'')+' · 目标 '+(completion.target_count||0)+' 个课次，已自动修复 '+(completion.repaired||0)+' 份';
  const last=s.last_run;
  $('last-run').textContent=last?.finished_at?`上次任务 ${last.stopped_by_user?'已停止':last.exit_code===0?'已完成':'已退出'} · ${dateTime(last.finished_at)}${last.exit_code ? ' · 退出码 '+last.exit_code:''}`:'';
  if(!scheduleDirty) { $('schedule-enabled').checked=s.schedule.enabled; $('schedule-time').value=s.schedule.time; }
  $('schedule-next').textContent=s.schedule.enabled?'下次运行：'+dateTime(s.schedule.next_at):'定时尚未开启';
  if(s.schedule.error) $('schedule-next').textContent+=' · '+s.schedule.error;
  const log=$('logs'), follow=log.scrollHeight-log.scrollTop-log.clientHeight<70;
  if(log.textContent!==s.logs) { log.textContent=s.logs||'尚无日志。开始处理后会记录实时日志。'; if(follow)log.scrollTop=log.scrollHeight; }
  $('sync-mail').disabled=s.mail_job.running; $('mail-status').textContent=s.mail_job.message+(s.mail_job.running&&s.mail_job.total?` ${s.mail_job.scanned}/${s.mail_job.total}`:'');
  if(s.mail_auto)$('mail-status').textContent+=' · 每 3 分钟自动检查新讲义';
}
async function refresh() {
  if(refreshing) return;
  refreshing=true;
  try { const previous=status; status=await api('/status'); csrf=status.token; renderStatus(status); if(currentPage==='library' || (previous?.mail_job.running && !status.mail_job.running)) await loadLibrary(); }
  catch(error) { $('connection').textContent='连接已断开'; $('connection').className='pill error-line'; $('run-detail').textContent='无法连接本机服务，请保持控制台服务运行。'; $('start').disabled=true; $('stop').disabled=true; }
  finally { refreshing=false; }
}
async function navigate(page) {
  currentPage=page;
  document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id==='page-'+page));
  document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  $('crumb').textContent=labels[page];
  try { if(page==='courses') await loadCourses(); if(page==='library') await loadLibrary(); if(page==='mail') await loadMailSettings(); if(page==='settings') await loadSettings(); }
  catch(error) { toast(error.message,true); }
}
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.page)));
$('start').addEventListener('click',()=>action($('start'),'/start'));
$('stop').addEventListener('click',()=>action($('stop'),'/stop'));
$('refresh').addEventListener('click',refresh);
$('cancel-completion').addEventListener('click',()=>action($('cancel-completion'),'/completion/cancel'));
$('view-attention').addEventListener('click',()=>openAttention().catch(error=>toast(error.message,true)));
[$('schedule-enabled'),$('schedule-time')].forEach(input=>input.addEventListener('change',()=>scheduleDirty=true));
$('save-schedule').addEventListener('click',async()=>{const button=$('save-schedule');button.disabled=true;try{const result=await api('/schedule',{enabled:$('schedule-enabled').checked,time:$('schedule-time').value});scheduleDirty=false;toast(result.message);await refresh();}catch(error){toast(error.message,true);}finally{button.disabled=false;}});
function renderSubscriptions() {
  $('selected-count').textContent=selections.size;
  $('course-dirty').textContent=dirtyCourses?'有未保存的更改':'已保存';
  $('subscriptions').innerHTML=selections.size?[...selections].map(id=>`<span class="chip">${escapeHTML(courseNames.get(id)||'课程 '+id)} <button data-remove="${escapeHTML(id)}" aria-label="取消订阅 ${escapeHTML(courseNames.get(id)||id)}">×</button></span>`).join(''):'<p class="muted">还没有订阅课程。可搜索课程或直接添加课程 ID。</p>';
  $('subscriptions').querySelectorAll('[data-remove]').forEach(button=>button.addEventListener('click',()=>{selections.delete(button.dataset.remove);dirtyCourses=true;renderSubscriptions();document.querySelectorAll('[data-course]').forEach(input=>input.checked=selections.has(input.dataset.course));}));
}
async function loadCourses() {
  const request=++courseRequest;
  const data=await api('/courses?q='+encodeURIComponent($('course-search').value)+'&term='+encodeURIComponent($('course-term').value));
  if(request!==courseRequest)return;
  if(!coursesLoaded){selections=new Set(data.selected);coursesLoaded=true;const selected=$('course-term').value;$('course-term').innerHTML='<option value="">全部学期</option>'+data.terms.map(term=>`<option value="${escapeHTML(term)}">${escapeHTML(term)}</option>`).join('');$('course-term').value=selected;}
  [...data.subscriptions,...data.catalog].forEach(course=>courseNames.set(course.course_id,course.title));
  renderSubscriptions();
  $('course-results').innerHTML=data.catalog.length?data.catalog.map(course=>`<article class="course-card ${selections.has(course.course_id)?'selected':''}"><label><input type="checkbox" data-course="${escapeHTML(course.course_id)}" ${selections.has(course.course_id)?'checked':''}><h3>${escapeHTML(course.title)}</h3><p>${escapeHTML(course.teacher||'教师待补充')}</p><p>${escapeHTML(course.term)} · ID ${escapeHTML(course.course_id)}</p></label></article>`).join(''):'<div class="empty"><strong>没有找到匹配课程</strong>尝试其他关键词，或直接输入课程 ID。</div>';
  $('course-results').querySelectorAll('[data-course]').forEach(input=>input.addEventListener('change',()=>{input.checked?selections.add(input.dataset.course):selections.delete(input.dataset.course);dirtyCourses=true;input.closest('article').classList.toggle('selected',input.checked);renderSubscriptions();}));
}
$('search-courses').addEventListener('click',()=>loadCourses().catch(error=>toast(error.message,true)));
$('course-search').addEventListener('keydown',event=>{if(event.key==='Enter')$('search-courses').click();});
$('course-term').addEventListener('change',()=>$('search-courses').click());
$('add-course').addEventListener('click',()=>{const id=$('manual-id').value.trim();if(!/^\d{1,20}$/.test(id))return toast('请输入数字课程 ID',true);selections.add(id);dirtyCourses=true;$('manual-id').value='';renderSubscriptions();});
$('save-courses').addEventListener('click',async()=>{const button=$('save-courses');button.disabled=true;try{const result=await api('/courses',{ids:[...selections]});dirtyCourses=false;renderSubscriptions();toast(result.message);}catch(error){toast(error.message,true);}finally{button.disabled=false;}});
async function loadLibrary() { [localLectures,mailMessages]=await Promise.all([api('/lectures'),api('/mail/messages')]); renderLibrary(); }
function libraryEntries() {
  const unique=new Map();
  const key=e=>JSON.stringify([e.course.trim().replace(/\s+/g,''),e.day,e.title.trim().replace(/\s+/g,'')]);
  // Mail is already sorted newest first. Unknown combined messages stay in the mailbox.
  for(const mail of mailMessages)for(const [index,lecture] of (mail.lectures||[]).entries()) {
    if(!lecture.date)continue;
    const e={...mail,kind:'mail',course:lecture.course_title,day:lecture.date,title:lecture.sub_title,lectureIndex:index};
    if(!unique.has(key(e)))unique.set(key(e),e);
  }
  for(const l of localLectures) {
    const e={...l,kind:'local',course:l.course_title||'未分类课程',day:/^\d{4}-\d{2}-\d{2}$/.test(l.date||'')?l.date:'',title:l.sub_title};
    const previous=unique.get(key(e));
    if(!previous||l.summary_chars||previous.kind==='local'&&!previous.summary_chars)unique.set(key(e),e);
  }
  return [...unique.values()];
}
function renderLibrary() {
  const all=libraryEntries(), query=$('library-search').value.toLowerCase(), source=$('library-source').value;
  const courseSelect=$('library-course'), selected=courseSelect.value;
  const names=[...new Set(all.map(e=>e.course))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  courseSelect.innerHTML='<option value="all">全部课程</option>'+names.map(name=>`<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`).join('');
  courseSelect.value=names.includes(selected)?selected:'all';
  const from=$('library-from').value,to=$('library-to').value,order=$('library-order').value;
  if(from&&to&&from>to) {
    $('library-filter-count').textContent='开始日期不能晚于结束日期，请调整日期范围。';
    $('library-items').replaceChildren();return;
  }
  const entries=all.filter(e=>(source==='all'||source===e.kind)&&(courseSelect.value==='all'||e.course===courseSelect.value)&&
    (e.course+' '+e.title+' '+(e.subject||'')).toLowerCase().includes(query)&&
    (!from||e.day&&e.day>=from)&&(!to||e.day&&e.day<=to));
  const grouped=new Map();
  for(const e of entries){if(!grouped.has(e.course))grouped.set(e.course,[]);grouped.get(e.course).push(e);}
  const collapsed=new Set([...$('library-items').querySelectorAll('details[data-library-course]')].filter(e=>!e.open).map(e=>e.dataset.libraryCourse));
  function card(e) {
    const readable=e.kind==='mail'||e.summary_chars||e.transcript_chars;
    const state=e.kind==='mail'?`${e.attachments} 个附件 · 独立课次讲义`:e.error_count?'需要关注 · '+(attentionStages[e.error_stage]||'处理异常'):e.summary_chars?'讲义已生成':e.transcript_chars?'已有转录':'等待处理';
    return `<article class="lecture-card"><span class="source">${e.kind==='mail'?'邮箱讲义':'本地课程'} / ${escapeHTML(e.day||'授课日期待补')}</span><h3>${escapeHTML(e.title)}</h3><p>${escapeHTML(state)}</p><button ${e.kind==='mail'?'data-mail':'data-lecture'}="${escapeHTML(e.kind==='mail'?e.id:e.sub_id)}" data-mail-index="${e.lectureIndex??''}" class="button outline" ${readable?'':'disabled'}>${e.kind==='mail'?'阅读邮件讲义':'阅读讲义'} ↗</button>${e.kind==='local'&&e.error_count?`<button class="attention-entry" data-attention="${escapeHTML(e.sub_id)}">查看问题详情 ↗</button>`:''}</article>`;
  }
  let content='';
  for(const course of names.filter(name=>grouped.has(name))) {
    const items=grouped.get(course).sort((a,b)=>{
      if(!a.day||!b.day)return a.day?-1:b.day?1:0;
      const date=a.day.localeCompare(b.day)*(order==='asc'?1:-1);
      return date||a.title.localeCompare(b.title,'zh-CN',{numeric:true});
    });
    const days=new Map();for(const item of items){if(!days.has(item.day))days.set(item.day,[]);days.get(item.day).push(item);}
    const ready=items.filter(e=>e.kind==='mail'||e.summary_chars||e.transcript_chars).length;
    content+=`<details class="library-course-group" data-library-course="${escapeHTML(course)}" ${collapsed.has(course)?'':'open'}><summary><strong>${escapeHTML(course)}</strong><span>${items.length} 项 · ${ready} 项可阅读</span></summary><div class="library-course-body">`;
    for(const [day,rows] of days)content+=`<section class="library-day"><h3>${escapeHTML(day||'授课日期待补')}<span>${rows.length} 项</span></h3><div class="library-grid">${rows.map(card).join('')}</div></section>`;
    content+='</div></details>';
  }
  $('library-items').innerHTML=content||'<div class="empty"><strong>暂无符合条件的讲义</strong>调整课程、来源或日期筛选，或等待课程处理完成。</div>';
  $('library-count').textContent=`${localLectures.filter(l=>l.summary_chars).length} 份本地讲义 · ${mailMessages.length} 封邮件`;
  $('library-filter-count').textContent=`当前显示 ${grouped.size} 门课程 · ${entries.length} 项课次记录，课程组可折叠。日期按授课时间分类，日期未知的邮件归入“授课日期待补”。`;
  $('library-items').querySelectorAll('[data-lecture]').forEach(button=>button.addEventListener('click',()=>openLecture(button.dataset.lecture).catch(error=>toast(error.message,true))));
  $('library-items').querySelectorAll('[data-mail]').forEach(button=>button.addEventListener('click',()=>openMail(button.dataset.mail,Number(button.dataset.mailIndex)).catch(error=>toast(error.message,true))));
  bindAttention($('library-items'));
}
$('library-search').addEventListener('input',renderLibrary);
for(const id of ['library-source','library-course','library-from','library-to','library-order'])$(id).addEventListener('change',renderLibrary);
$('library-reset').addEventListener('click',()=>{
  $('library-search').value='';$('library-source').value='all';$('library-course').value='all';
  $('library-from').value='';$('library-to').value='';$('library-order').value='desc';renderLibrary();
});
const attentionStages={no_video:'暂无课程回放',no_audio:'未找到音轨',transcribe:'语音识别',summarize:'讲义生成',pipeline:'课程处理',no_content:'课堂内容不足'};
function bindAttention(container){container.querySelectorAll('[data-attention]').forEach(button=>button.addEventListener('click',()=>openAttentionDetail(button.dataset.attention).catch(error=>toast(error.message,true))));}
async function openAttention(){
  const rows=await api('/attention');showReader('需要关注的课次','课程处理 · 问题列表');
  $('reader-body').innerHTML=rows.length?`<p>${rows.length} 个课次需要关注，点击查看具体原因和已有结果。</p><div class="attention-list">${rows.map(row=>`<article class="attention-card"><h3>${escapeHTML(row.course_title)}</h3><p>${escapeHTML(row.sub_title)} · ${escapeHTML(row.date||'日期待补')}</p><p>${escapeHTML(attentionStages[row.error_stage]||'处理异常')} · 累计 ${row.error_count} 条错误记录</p><button class="button outline" data-attention="${escapeHTML(row.sub_id)}">查看详细内容 ↗</button></article>`).join('')}</div>`:'<div class="empty"><strong>当前没有需要关注的课次</strong>课程处理结果会持续更新。</div>';
  bindAttention($('reader-body'));
}
async function openAttentionDetail(id){
  const rows=await api('/attention'),row=rows.find(r=>String(r.sub_id)===String(id));
  if(!row){toast('该课次的问题已消除');return openAttention();}
  showReader(row.course_title+' · '+row.sub_title,'需要关注 · '+(attentionStages[row.error_stage]||'处理异常'));
  const back=document.createElement('button');back.className='button outline';back.textContent='← 返回关注列表';back.addEventListener('click',()=>openAttention().catch(error=>toast(error.message,true)));$('reader-files').append(back);
  if(row.summary_chars||row.transcript_chars){const read=document.createElement('button');read.className='button outline';read.textContent=row.summary_chars?'阅读已有讲义':'查看已保存的原始转写';read.addEventListener('click',()=>openLecture(id).catch(error=>toast(error.message,true)));$('reader-files').append(read);}
  const suggestion=row.error_stage==='no_video'?'目前没有可播放的课程回放。请在课程平台确认视频是否已发布，这类记录不表示程序崩溃。':row.error_stage==='no_audio'?'回放未检测到可用音轨，请确认原视频是否有声音。':row.transcript_chars&&!row.summary_chars?'原始转写已保存，讲义尚未生成。检查错误信息对应的模型连接或处理问题，恢复后可复用转写。':'请结合错误原因和运行日志确认网络、课程回放或模型运行情况。';
  $('reader-body').innerHTML=`<dl class="attention-facts"><dt>授课日期</dt><dd>${escapeHTML(row.date||'待补')}</dd><dt>失败阶段</dt><dd>${escapeHTML(attentionStages[row.error_stage]||row.error_stage||'未记录')}</dd><dt>累计错误记录</dt><dd>${row.error_count}</dd><dt>重试状态</dt><dd>${row.error_count>=3?'已达到默认自动重试上限，需处理原因后解除暂停':'尚未达到默认自动重试上限'}</dd><dt>已保存转写</dt><dd>${row.transcript_chars?row.transcript_chars+' 字符':'尚无转写'}</dd><dt>已生成讲义</dt><dd>${row.summary_chars?row.summary_chars+' 字符':'尚无讲义'}</dd><dt>邮件回执</dt><dd>${row.emailed_at?'发送记录时间：'+dateTime(row.emailed_at):'暂无成功发送记录'}</dd></dl><h3>错误详情</h3><pre class="attention-error">${escapeHTML(row.error_msg)}</pre><h3>处理建议</h3><p>${escapeHTML(suggestion)}</p>`;
}
function showReader(title,source) {if(window.MathJax?.typesetClear)MathJax.typesetClear([$('reader-body')]);$('reader-title').textContent=title;$('reader-source').textContent=source;$('reader-files').replaceChildren();$('reader-body').replaceChildren();$('reader-body').scrollTop=0;if(!$('reader').open)$('reader').showModal();}
$('close-reader').addEventListener('click',()=>$('reader').close());
$('reader').addEventListener('click',event=>{
  if(event.target!==$('reader'))return;
  const bounds=$('reader').getBoundingClientRect();
  if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)$('reader').close();
});
async function openLecture(id) {
  const lecture=await api('/lecture/'+encodeURIComponent(id)); showReader(lecture.course_title+' · '+lecture.sub_title,lecture.summary?'本地课程讲义':'原始语音转写 · 讲义尚未生成');
  $('reader-body').innerHTML=DOMPurify.sanitize(marked.parse(lecture.summary||lecture.transcript||''));
  const link=document.createElement('a');link.className='button outline';link.textContent='下载 Markdown';link.download=lecture.course_title+'-'+lecture.sub_title+'.md';const objectURL=URL.createObjectURL(new Blob([lecture.summary||lecture.transcript||''],{type:'text/markdown;charset=utf-8'}));link.href=objectURL;$('reader-files').append(link);$('reader').addEventListener('close',()=>URL.revokeObjectURL(objectURL),{once:true});
  if(window.MathJax?.typesetPromise){await MathJax.startup.promise;await MathJax.typesetPromise([$('reader-body')]);}
}
async function openMail(id, lectureIndex=null) {
  const mail=await api('/mail/message/'+id);const section=lectureIndex===null?null:mailMessages.find(m=>m.id===id)?.lectures?.[lectureIndex];showReader(section?section.course_title+' · '+section.sub_title:mail.subject,'邮箱讲义 · '+(section?.date||mail.date));
  const frame=document.createElement('iframe');frame.title='课程邮件正文';frame.sandbox='';frame.src=lectureIndex===null?'/api/mail/body/'+id:'/api/mail/lecture/'+id+'/'+lectureIndex;$('reader-body').append(frame);
  for(const file of mail.attachments){const link=document.createElement('a');link.className='button outline';link.textContent=file.filename+' ↗';link.href='/api/mail/attachment/'+file.id;link.target='_blank';link.rel='noopener';$('reader-files').append(link);}
}
async function loadMailSettings() {
  const settings=await api('/mail/settings');
  $('mail-account').value=settings.account;$('mail-host').value=settings.host;$('mail-port').value=settings.port;$('mail-password').value='';
  const qq=/@(qq|foxmail)\.com$/i.test(settings.account);
  $('mail-provider-title').textContent=qq?'QQ 邮箱连接':'学校邮箱连接';
  $('password-state').textContent=settings.password_set?(qq?'授权码已保存；留空可保留现有授权码。':'专用密码已保存；留空可保留现有密码。'):(qq?'尚未保存 QQ 邮箱授权码。':'尚未保存客户端专用密码。');
  $('mail-password').placeholder=qq?'填写 QQ 邮箱 IMAP/SMTP 授权码':'填写邮箱客户端专用密码';
  $('mail-help').innerHTML=qq?'<strong>QQ 邮箱如何开通？</strong><p>登录 QQ 邮箱，在邮箱设置中开启 IMAP/SMTP 服务并获取授权码。这里使用授权码登录。</p><a href="https://mail.qq.com/" target="_blank" rel="noopener">打开 QQ 邮箱 ↗</a>':'<strong>复旦云邮箱如何开通？</strong><p>登录云邮箱，在“设置 → 收发信设置”中开启客户端协议，再到“设置 → 邮箱绑定”生成客户端专用密码。</p><a href="https://mail.m.fudan.edu.cn/" target="_blank" rel="noopener">打开复旦云邮箱 ↗</a>';
}
$('mail-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const result=await api('/mail/settings',{account:$('mail-account').value,host:$('mail-host').value,port:Number($('mail-port').value),password:$('mail-password').value});$('mail-password').value='';toast(result.message);await loadMailSettings();}catch(error){toast(error.message,true);}finally{button.disabled=false;}});
$('sync-mail').addEventListener('click',()=>action($('sync-mail'),'/mail/sync'));
$('eml-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;if(file.size>30*1024*1024){toast('单封邮件不能超过 30 MB',true);event.target.value='';return;}try{const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=16384)binary+=String.fromCharCode(...bytes.subarray(i,i+16384));const result=await api('/mail/upload',{content:btoa(binary)});toast(result.message);await loadLibrary();}catch(error){toast(error.message,true);}finally{event.target.value='';}});
refresh();setInterval(refresh,4000);

let settingsDirty=false;
const settingsGroups=[
  {title:'学校账号',description:'用于登录复旦 WebVPN 与课程平台。',fields:[['StuId','学号','text'],['UISPsw','登录密码','password']]},
  {title:'模型 API',description:'填入已开通服务的密钥。多个服务同时配置时，项目依次尝试 ModelScope、DeepSeek、Gemini。API 地址留空保留原设置，未配置时使用项目默认地址。',fields:[['DEEPSEEK_API_KEY','DeepSeek API Key','password'],['DEEPSEEK_BASE_URL','DeepSeek API 地址','url','https://api.deepseek.com/v1'],['DASHSCOPE_API_KEY','ModelScope API Key','password'],['DASHSCOPE_BASE_URL','ModelScope API 地址','url','https://api-inference.modelscope.cn/v1/'],['GEMINI_API_KEY','Gemini API Key','password'],['GEMINI_BASE_URL','Gemini API 地址','url','https://generativelanguage.googleapis.com/v1beta/openai/']]},
  {title:'邮箱发送与讲义导入',description:'QQ 邮箱使用 IMAP/SMTP 授权码。收件邮箱可填写多个，以逗号分隔；发件邮箱和导入账号分别设置。',fields:[['SMTP_EMAIL','发件邮箱','email'],['SMTP_PASSWORD','SMTP 授权码','password'],['RECEIVER_EMAILS','课程收件邮箱','text'],['IMAP_ACCOUNT','讲义导入邮箱','email'],['IMAP_PASSWORD','IMAP 授权码','password'],['IMAP_HOST','IMAP 服务器','text','imap.qq.com'],['IMAP_PORT','IMAP 端口','number','993']]},
  {title:'数据加密',description:'用于项目的加密导出与原查看器。更换后，旧加密文件仍需使用旧密钥解密，请保留原密钥。',fields:[['DB_ENCRYPTION_KEY','数据加密密钥','password']]}
];
async function loadSettings(){
  if(settingsDirty)return;
  const config=await api('/settings');
  $('settings-fields').innerHTML=settingsGroups.map(group=>`<section class="panel settings-panel"><h3>${group.title}</h3><p>${group.description}</p>${group.fields.map(([key,label,type,fallback])=>`<label for="setting-${key}">${label}<input id="setting-${key}" name="${key}" type="${type}" autocomplete="${type==='password'?'new-password':'off'}" ${type==='number'?'min="1" max="65535"':''} value="${escapeHTML(config.values[key]||'')}" placeholder="${config.configured[key]?'已配置，留空保留':escapeHTML(fallback||'尚未配置，请填写')}"><small>${config.configured[key]?'已保存配置':'尚未配置'}</small></label>`).join('')}</section>`).join('');
}
$('settings-form').addEventListener('input',()=>{settingsDirty=true;$('settings-result').textContent='有未保存的修改';});
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('save-settings');button.disabled=true;
  try{
    const fields={};for(const input of $('settings-form').querySelectorAll('input[name]'))if(input.value.trim())fields[input.name]=input.value;
    const result=await api('/settings',{fields});settingsDirty=false;await loadSettings();$('settings-result').textContent=result.message;toast('个人设置已保存');
  }catch(error){$('settings-result').textContent=error.message;toast(error.message,true);}
  finally{button.disabled=false;}
});
