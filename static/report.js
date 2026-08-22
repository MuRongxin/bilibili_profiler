// 报告页前端逻辑（原 web.py VIDEO_JS 平移；数据经 window.__DATA__ 注入，本文件须在其后加载）
// 页面数据由服务端内联 <script>window.__DATA__</script> 注入（工程结构改造，替代占位符 .replace）
const PAGE_DATA = window.__DATA__ || {};

// 标签页切换
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
    // 标签页写入 URL hash（spec 5）：刷新/分享链接可回到同一标签页
    if (location.hash !== '#tab=' + name) history.replaceState(null, '', '#tab=' + name);
}

// 从 URL hash 还原标签页（仅接受存在的标签名）
function restoreTabFromHash() {
    const m = location.hash.match(/tab=(\w+)/);
    if (m && document.querySelector('.tab-btn[data-tab="' + m[1] + '"]')) switchTab(m[1]);
}

// 概览图表（默认宽高比自适应，不写死高度）
const chartData = PAGE_DATA.chart;
new Chart(document.getElementById('levelChart'), {type:'bar',
    data:{labels:chartData.level_labels, datasets:[{label:'人数', data:chartData.level_data, backgroundColor:'#00a1d6', borderRadius:6}]},
    options:{responsive:true, plugins:{legend:{display:false}}}});
new Chart(document.getElementById('spamChart'), {type:'doughnut',
    data:{labels:['低风险','中风险','高风险'], datasets:[{data:chartData.spam_data, backgroundColor:['#4caf50','#ff9800','#f44336']}]},
    options:{responsive:true}});
new Chart(document.getElementById('tagChart'), {type:'bar',
    data:{labels:chartData.tag_labels, datasets:[{label:'出现次数', data:chartData.tag_data, backgroundColor:'#ff9f43', borderRadius:6}]},
    options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
if (chartData.region_labels.length) {
    new Chart(document.getElementById('regionChart'), {type:'bar',
        data:{labels:chartData.region_labels, datasets:[{label:'人数', data:chartData.region_data, backgroundColor:'#fb7299', borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
}

// 弹幕密度时间轴（概览页宽幅；无全量弹幕数据的旧视频无此 canvas）
// 点击柱条跳转视频对应时段核验（P1-a）：starts 为每桶起始秒数
const densityData = PAGE_DATA.density;
const densityCanvas = document.getElementById('densityChart');
if (densityCanvas && densityData) {
    new Chart(densityCanvas, {type:'bar',
        data:{labels:densityData.labels, datasets:[{label:'弹幕数', data:densityData.data, backgroundColor:'#00a1d6', borderRadius:2}]},
        options:{responsive:true, aspectRatio:4,
            onHover:(e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
            onClick:(e, els) => {
                if (!els.length) return;
                const t = (densityData.starts || [])[els[0].index] || 0;
                window.open('https://www.bilibili.com/video/' + PAGE_DATA.bvid + '?t=' + t, '_blank', 'noopener');
            },
            plugins:{legend:{display:false}, tooltip:{callbacks:{title: items => '视频时间 ' + items[0].label + '（点击跳转）'}}},
            scales:{x:{ticks:{autoSkip:true, maxTicksLimit:20}}, y:{beginAtZero:true}}}});
}

// 解析质量区块（概览页）：解析方式分布 + 置信度分布
const rqData = PAGE_DATA.resolveQuality;
if (rqData && document.getElementById('rqMethodChart')) {
    new Chart(document.getElementById('rqMethodChart'), {type:'doughnut',
        data:{labels:rqData.method_labels, datasets:[{data:rqData.method_data,
              backgroundColor:['#00a1d6','#66bb6a','#ff9f43','#ab47bc','#ef5350','#8d6e63','#90a4ae']}]},
        options:{responsive:true}});
    const confColors = {'高':'#4caf50','中':'#ff9800','低':'#f44336','无':'#bdbdbd'};
    new Chart(document.getElementById('rqConfChart'), {type:'bar',
        data:{labels:rqData.conf_labels, datasets:[{label:'人数', data:rqData.conf_data,
              backgroundColor:rqData.conf_labels.map(c => confColors[c] || '#bdbdbd'), borderRadius:6}]},
        options:{responsive:true, plugins:{legend:{display:false}}}});
}

// 问题弹幕类别分布小图（弹幕浏览器统计面板；无弹幕数据的旧视频无此 canvas）
const dmCatData = PAGE_DATA.categories;
const dmCatColors = PAGE_DATA.categoryColors;
const dmCatCanvas = document.getElementById('dmCatChart');
if (dmCatCanvas) {
    const catLabels = Object.keys(dmCatData);
    new Chart(dmCatCanvas, {type:'bar',
        data:{labels:catLabels, datasets:[{label:'命中人数', data:catLabels.map(k => dmCatData[k]),
              backgroundColor:catLabels.map(k => dmCatColors[k] || '#999'), borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
}

// 弹幕模式分布小图（弹幕浏览器统计面板；mode 已入库：滚动/顶部/底部/其他）
const dmModeData = PAGE_DATA.dmMode;
const dmModeCanvas = document.getElementById('dmModeChart');
if (dmModeCanvas && dmModeData) {
    new Chart(dmModeCanvas, {type:'doughnut',
        data:{labels:Object.keys(dmModeData), datasets:[{data:Object.values(dmModeData),
              backgroundColor:['#00a1d6','#fb7299','#ff9f43','#90a4ae']}]},
        options:{responsive:true}});
}

// 高回复评论：回复树默认折叠到固定高度，展开/折叠切换
function hotToggle(btn) {
    const box = btn.previousElementSibling;
    const collapsed = box.classList.toggle('collapsed');
    btn.textContent = collapsed ? '展开全部回复 ▾' : '收起回复 ▴';
}

// 误报标记（P2-a）：问题弹幕（kind=dm，target=内容）/问题评论（kind=cmt，target=rpid）
// 切换人工误报标记；标记后该条不再计入聚合与用户疑似分，可撤销
function fpToggle(btn) {
    const kind = btn.dataset.kind, target = btn.dataset.target;
    const willMark = !btn.classList.contains('fp-btn-marked');
    const shown = target.length > 50 ? target.slice(0, 50) + '…' : target;
    const msg = willMark
        ? '将该条' + (kind === 'dm' ? '弹幕内容' : '评论') + '标记为误报？标记后不再计入聚合。\n\n' + shown
        : '撤销该条的误报标记？撤销后重新计入聚合。\n\n' + shown;
    if (!confirm(msg)) return;
    btn.disabled = true;
    fetch('/api/video/' + encodeURIComponent(BVID) + '/false_positive', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind: kind, target: target})
    }).then(r => r.json().then(j => ({ok: r.ok, j})))
      .then(({ok, j}) => {
          if (!ok) { alert(j.error || '标记失败'); btn.disabled = false; return; }
          location.reload();   // 聚合已变（页缓存服务端已失效），整页刷新最简且一致
      })
      .catch(() => { alert('网络错误'); btn.disabled = false; });
}

// UP主悬停词云弹窗
const upWcData = PAGE_DATA.upWordcloud;
const popup = document.getElementById('wc-popup');
const popupCanvas = document.getElementById('wc-popup-canvas');
document.querySelectorAll('.up-chip').forEach(chip => {
    chip.addEventListener('mouseenter', function() {
        const upId = this.dataset.upid;
        const data = upWcData[upId];
        if (!data || data.length === 0) return;
        const rect = this.getBoundingClientRect();
        popup.style.display = 'block';
        popup.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
        popup.style.top = (rect.bottom + 8) + 'px';
        const maxW = Math.max(...data.map(d => d[1]));
        const minW = Math.min(...data.map(d => d[1]));
        const scaled = data.map(d => [d[0], 10 + (d[1] - minW) / Math.max(maxW - minW, 1) * 50]);
        WordCloud(popupCanvas, {list: scaled, gridSize: 10, weightFactor: 1, fontFamily: 'sans-serif',
            color: () => ['#00a1d6','#fb7299','#ff9f43','#6c5ce7','#2e7d32'][Math.floor(Math.random()*5)],
            rotateRatio: 0, backgroundColor: '#ffffff', shape: 'circle', clearCanvas: true});
    });
    chip.addEventListener('mouseleave', function() { popup.style.display = 'none'; });
});

// ===== 用户画像：筛选 + 搜索防抖 + 排序 + 前端分页（全部读卡片 data-* 属性，不再做 DOM 位置解析） =====
const USER_PAGE_SIZE = 24;
const userState = {filter: 'all', kw: '', sort: 'risk', page: 1};
let userSearchTimer = null;
let userCards = [];   // 缓存卡片元素与解析后的数据，避免每次 querySelectorAll 重读 DOM

function userCardData(card) {
    return {
        el: card,
        level: parseInt(card.dataset.level) || 0,
        vip: card.dataset.vip === 'true',
        spam: card.dataset.spam || '低',
        official: card.dataset.official === 'true',
        isUp: card.dataset.isUp === 'true',
        spamScore: parseFloat(card.dataset.spamScore) || 0,
        dmCount: parseInt(card.dataset.danmakuCount) || 0,
        fans: parseInt(card.dataset.fans) || 0,
        name: (card.querySelector('.username')?.textContent || '').toLowerCase(),
        uid: (card.querySelector('.uid')?.textContent || '').toLowerCase(),
        riskIdx: 0,   // 服务端 sort_profiles_by_risk 顺序下标，initUserCards 中赋值
    };
}

function initUserCards() {
    const grid = document.getElementById('userGrid');
    if (!grid) return;
    userCards = Array.from(grid.querySelectorAll('.user-card')).map((el, i) => {
        const d = userCardData(el);
        d.riskIdx = i;
        return d;
    });
    // 昵称/UID 搜索：300ms 防抖
    document.getElementById('userSearch').addEventListener('input', () => {
        clearTimeout(userSearchTimer);
        userSearchTimer = setTimeout(() => {
            userState.kw = document.getElementById('userSearch').value.trim().toLowerCase();
            userState.page = 1;
            renderUserCards();
        }, 300);
    });
    document.getElementById('userSort').addEventListener('change', function() {
        userState.sort = this.value;
        userState.page = 1;
        renderUserCards();
    });
    renderUserCards();
}

function userFilter(type, btn) {
    userState.filter = type;
    userState.page = 1;
    document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderUserCards();
}

function userMatchFilter(d) {
    switch (userState.filter) {
        case 'all': return true;
        case 'high-level': return d.level >= 5;
        case 'vip': return d.vip;
        case 'official': return d.official;
        case 'spam': return d.spam !== '低';
        case 'creator': return d.isUp;
    }
    return true;
}

function userSortCmp(a, b) {
    switch (userState.sort) {
        case 'spam-score': return b.spamScore - a.spamScore || a.riskIdx - b.riskIdx;
        case 'danmaku': return b.dmCount - a.dmCount || a.riskIdx - b.riskIdx;
        case 'fans': return b.fans - a.fans || a.riskIdx - b.riskIdx;
        default: return a.riskIdx - b.riskIdx;   // risk：保持服务端风险序
    }
}

function renderUserCards() {
    const grid = document.getElementById('userGrid');
    if (!grid) return;
    const hit = userCards.filter(d => userMatchFilter(d) &&
        (!userState.kw || d.name.includes(userState.kw) || d.uid.includes(userState.kw)));
    hit.sort(userSortCmp);
    hit.forEach(d => grid.appendChild(d.el));   // appendChild 移动已挂载节点完成重排
    const pages = Math.max(1, Math.ceil(hit.length / USER_PAGE_SIZE));
    userState.page = Math.min(userState.page, pages);
    const start = (userState.page - 1) * USER_PAGE_SIZE;
    const showSet = new Set(hit.slice(start, start + USER_PAGE_SIZE).map(d => d.el));
    userCards.forEach(d => { d.el.style.display = showSet.has(d.el) ? '' : 'none'; });
    document.getElementById('userResultCount').textContent =
        '共 ' + userCards.length + ' 人 · 命中 ' + hit.length + ' 人';
    document.getElementById('userEmpty').style.display = (userCards.length && !hit.length) ? 'block' : 'none';
    renderUserPager(pages);
}

function renderUserPager(pages) {
    const pager = document.getElementById('userPager');
    if (pages <= 1) { pager.innerHTML = ''; return; }
    let html = '<button class="pager-btn" ' + (userState.page <= 1 ? 'disabled' : '') +
        ' onclick="userPage(' + (userState.page - 1) + ')">上一页</button>';
    for (let p = 1; p <= pages; p++) {
        html += '<button class="pager-btn' + (p === userState.page ? ' active' : '') +
            '" onclick="userPage(' + p + ')">' + p + '</button>';
    }
    html += '<button class="pager-btn" ' + (userState.page >= pages ? 'disabled' : '') +
        ' onclick="userPage(' + (userState.page + 1) + ')">下一页</button>';
    pager.innerHTML = html;
}

function userPage(p) {
    userState.page = p;
    renderUserCards();
    // 翻页后自动回到画像区顶部（fix：避免停在上次滚动位置，看不到新页开头）
    const bar = document.querySelector('#tab-users .filter-bar');
    (bar || document.getElementById('userGrid')).scrollIntoView({block: 'start'});
}

// 弹幕浏览器点击发送者跳转到用户画像卡片（锚点 id="uid-{uid}"，spec 4）
// 分页感知：重置筛选/搜索后翻到目标卡片所在页，再滚动高亮
function gotoUser(uid) {
    switchTab('users');
    clearTimeout(userSearchTimer);   // 清掉滞后的搜索防抖回调，防止抵消下面的翻页
    const el = document.getElementById('uid-' + uid);
    if (!el) return;
    userState.filter = 'all';
    userState.kw = '';
    const searchEl = document.getElementById('userSearch');
    if (searchEl) searchEl.value = '';
    document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.filter === 'all'));
    const d = userCards.find(c => c.el === el);
    if (d) {
        const hit = userCards.filter(x => userMatchFilter(x));
        hit.sort(userSortCmp);
        userState.page = Math.max(1, Math.floor(hit.indexOf(d) / USER_PAGE_SIZE) + 1);
        renderUserCards();
    }
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.style.boxShadow = '0 0 0 3px #00a1d6';
    setTimeout(() => { el.style.boxShadow = ''; }, 2000);
}

// 弹幕浏览器（JSON API + 前端渲染当前页，spec 4）
const BVID = PAGE_DATA.bvid;
const dmState = {page: 1};
let dmTimer = null;

function dmParams() {
    const p = new URLSearchParams();
    const search = document.getElementById('dmSearch').value.trim();
    const sender = document.getElementById('dmSender').value.trim();
    const cat = document.getElementById('dmCategory').value;
    const spam = document.getElementById('dmSpam').value;
    if (search) p.set('search', search);
    if (sender) p.set('sender', sender);
    if (cat) p.set('category', cat);
    if (spam) p.set('spam', spam);
    if (document.getElementById('dmAnalyzed').checked) p.set('analyzed', '1');
    p.set('sort', document.getElementById('dmSort').value);
    p.set('order', document.getElementById('dmOrder').value);
    p.set('page_size', document.getElementById('dmPageSize').value);
    p.set('page', dmState.page);
    return p.toString();
}

// 弹幕浏览器状态写入 query 参数（spec 5）：搜索词/发送者/筛选/排序/页码/每页条数
function dmSyncUrl() {
    const u = new URL(location.href);
    ['search', 'sender', 'category', 'spam', 'sort', 'order', 'page', 'page_size', 'analyzed']
        .forEach(k => u.searchParams.delete(k));
    new URLSearchParams(dmParams()).forEach((v, k) => u.searchParams.set(k, v));
    history.replaceState(null, '', u.toString());
}

// 页面加载时从 query 参数还原弹幕浏览器控件与页码
function dmRestoreFromUrl() {
    const q = new URLSearchParams(location.search);
    const setVal = (id, key) => { if (q.has(key)) document.getElementById(id).value = q.get(key); };
    setVal('dmSearch', 'search');
    setVal('dmSender', 'sender');
    setVal('dmCategory', 'category');
    setVal('dmSpam', 'spam');
    setVal('dmSort', 'sort');
    setVal('dmOrder', 'order');
    setVal('dmPageSize', 'page_size');
    if (q.get('analyzed') === '1') document.getElementById('dmAnalyzed').checked = true;
    const pg = parseInt(q.get('page'));
    if (!isNaN(pg) && pg >= 1) dmState.page = pg;
}

function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtVideoTime(sec) {
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function loadDanmaku() {
    const err = document.getElementById('dmError');
    const spinner = document.getElementById('dmSpinner');
    err.style.display = 'none';
    spinner.style.display = 'block';
    fetch('/api/video/' + encodeURIComponent(BVID) + '/danmaku?' + dmParams())
        .then(r => {
            if (!r.ok) return r.json().then(j => Promise.reject(new Error(j.error || ('HTTP ' + r.status))));
            return r.json();
        })
        .then(data => {
            const tbody = document.getElementById('dmTbody');
            tbody.innerHTML = data.rows.map(row => {
                const sender = row.uid
                    ? '<a onclick="gotoUser(' + row.uid + ')">' + escHtml(row.name || row.uid) + '</a><br><span class="dm-time">UID:' + row.uid + '</span>'
                    : '<span class="dm-time">' + escHtml(row.mid_hash) + '</span>';
                const dup = row.dup_count > 1 ? ' <span class="dm-time">×' + row.dup_count + '</span>' : '';
                const dot = row.color ? '<span class="dm-dot" style="background:' + escHtml(row.color) +
                    '" title="' + escHtml(row.color) + '"></span>' : '';
                const cats = (row.categories || []).map(c =>
                    '<span style="display:inline-block;background:' + (dmCatColors[c] || '#999') +
                    ';color:#fff;font-size:12px;border-radius:4px;padding:1px 8px;margin:1px 2px;">' +
                    escHtml(c) + '</span>').join('');
                const chk = '<input type="checkbox" class="dm-check" data-mid="' + escHtml(row.mid_hash) + '"' +
                    (dmSelected.has(row.mid_hash) ? ' checked' : '') + '>';
                return '<tr><td>' + chk + '</td><td>' + dot + escHtml(row.content) + dup + '</td><td>' + sender + '</td><td>' +
                    fmtVideoTime(row.first_video_time) + '</td><td>' +
                    new Date(row.first_send_time * 1000).toLocaleString() + '</td><td>' + cats + '</td><td>' +
                    escHtml(row.spam_level) + '</td></tr>';
            }).join('') || '<tr><td colspan="7" class="empty-note">无匹配弹幕</td></tr>';
            dmBindChecks();
            const pageSize = data.page_size || 100;
            const pages = Math.max(1, Math.ceil(data.total / pageSize));
            document.getElementById('dmPageInfo').textContent =
                '第 ' + data.page + ' / ' + pages + ' 页（共 ' + data.total + ' 行）';
            document.getElementById('dmPrev').disabled = data.page <= 1;
            document.getElementById('dmNext').disabled = data.page >= pages;
            dmSyncUrl();
        })
        .catch(e => {
            document.getElementById('dmErrorText').textContent = '弹幕加载失败: ' + e.message;
            err.style.display = 'flex';
        })
        .finally(() => { spinner.style.display = 'none'; });
}

// 页码输入跳转（spec 3）
function dmGotoPage() {
    const v = parseInt(document.getElementById('dmGoto').value);
    if (!isNaN(v) && v >= 1) {
        dmState.page = v;
        loadDanmaku();
    }
}

// 每页条数切换（50/100/200）：回到第 1 页
function dmPageSizeChange() {
    dmState.page = 1;
    loadDanmaku();
}

function dmPage(delta) { dmState.page = Math.max(1, dmState.page + delta); loadDanmaku(); }
function dmReload() { dmState.page = 1; loadDanmaku(); }

// 统计面板 Top10 点击 → 切到弹幕浏览器并筛选该发送者（spec 4）
function filterSender(midHash) {
    switchTab('danmaku');
    document.getElementById('dmSender').value = midHash;
    dmReload();
}

// 用户画像初始化（筛选/排序/分页/搜索防抖）
initUserCards();

// 事件绑定（旧视频无全量弹幕时无 dmTbody，跳过）
if (document.getElementById('dmTbody')) {
    document.getElementById('dmSearch').addEventListener('input', () => { clearTimeout(dmTimer); dmTimer = setTimeout(dmReload, 400); });
    document.getElementById('dmSender').addEventListener('input', () => { clearTimeout(dmTimer); dmTimer = setTimeout(dmReload, 400); });
    ['dmCategory', 'dmSpam', 'dmSort', 'dmOrder', 'dmAnalyzed'].forEach(id =>
        document.getElementById(id).addEventListener('change', dmReload));
    document.getElementById('dmPageSize').addEventListener('change', dmPageSizeChange);
    document.getElementById('dmGoto').addEventListener('keydown', e => { if (e.key === 'Enter') dmGotoPage(); });
    document.getElementById('dmCheckAll').addEventListener('change', function() {
        document.querySelectorAll('.dm-check').forEach(c => {
            c.checked = this.checked;
            if (this.checked) dmSelected.add(c.dataset.mid); else dmSelected.delete(c.dataset.mid);
        });
        dmUpdateAnalyzeStatus();
    });
    dmRestoreFromUrl();
    loadDanmaku();
}

// 手动勾选分析（spec B）：勾选状态跨页保留在 dmSelected（key=mid_hash，天然去重）；
// job_id 存 sessionStorage，刷新页面后可继续轮询（spec 5；服务重启 job 丢失则提示后清除）
const dmSelected = new Set();
const DM_JOB_KEY = 'dmJob_' + BVID;

function dmUpdateAnalyzeStatus(text) {
    const el = document.getElementById('dmAnalyzeStatus');
    if (!el) return;  // 旧视频无弹幕面板时无此元素
    el.textContent = text !== undefined ? text
        : (dmSelected.size ? '已选 ' + dmSelected.size + ' 个发送者' : '');
}
function dmBindChecks() {
    document.querySelectorAll('.dm-check').forEach(c =>
        c.addEventListener('change', function() {
            if (this.checked) dmSelected.add(this.dataset.mid); else dmSelected.delete(this.dataset.mid);
            dmUpdateAnalyzeStatus();
        }));
}
function startAnalysis() {
    const mids = Array.from(dmSelected);
    if (!mids.length) { dmUpdateAnalyzeStatus('请先勾选弹幕行'); return; }
    document.getElementById('dmFailBar').style.display = 'none';   // 新 job 启动时收起旧失败条
    dmUpdateAnalyzeStatus('正在启动分析...');
    fetch('/api/video/' + encodeURIComponent(BVID) + '/analyze', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mid_hashes: mids})
    }).then(r => r.json().then(j => ({ok: r.ok, j})))
      .then(({ok, j}) => {
          if (!ok) { dmUpdateAnalyzeStatus('启动失败: ' + (j.error || ('HTTP 未知错误'))); return; }
          sessionStorage.setItem(DM_JOB_KEY, j.job_id);
          // job 轮询期间禁用发起按钮：DM_JOB_KEY 单槽，防止并发 job 互相覆盖跟踪状态
          document.getElementById('dmAnalyzeBtn').disabled = true;
          dmSelected.clear();
          document.querySelectorAll('.dm-check').forEach(c => c.checked = false);
          pollJob(j.job_id);
      })
      .catch(e => dmUpdateAnalyzeStatus('启动失败: ' + e.message));   // 发起失败按钮保持可用
}
let jobPollFails = 0;   // 连续轮询失败计数：>=5 判定状态接口不可用，停止轮询并提示（spec 错误处理）
function pollJob(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            jobPollFails = 0;
            if (j.error) {  // 服务重启 job 丢失
                dmUpdateAnalyzeStatus('任务状态查询失败: ' + j.error + '（数据可能已部分落库）');
                sessionStorage.removeItem(DM_JOB_KEY);
                document.getElementById('dmAnalyzeBtn').disabled = false;
                return;
            }
            dmUpdateAnalyzeStatus('分析中 ' + j.done + '/' + j.total + (j.current ? '　' + j.current : ''));
            if (j.finished) {
                sessionStorage.removeItem(DM_JOB_KEY);
                document.getElementById('dmAnalyzeBtn').disabled = false;
                const errs = j.errors || [];
                // 新卡片需服务端渲染才有 DOM：记录待高亮 UID 与结果文案，重载后由加载恢复逻辑展示
                sessionStorage.setItem('dmFlash_' + BVID, JSON.stringify(j.results || []));
                sessionStorage.setItem('dmFlashMsg_' + BVID,
                    '手动分析完成: 成功 ' + (j.results || []).length + '/' + j.total +
                    (errs.length ? '（' + errs.length + ' 条失败）' : ''));
                if (errs.length) {
                    // 有失败项：不自动重载，展示失败明细条，由用户选「重试失败项」或「刷新查看结果」
                    renderJobFailures(errs);
                } else {
                    saveViewState();
                    location.reload();
                }
            } else {
                setTimeout(() => pollJob(jobId), 2000);
            }
        })
        .catch(() => {
            jobPollFails++;
            if (jobPollFails >= 5) {
                dmUpdateAnalyzeStatus('进度查询失败，请稍后刷新页面（job 可能仍在后台运行）');
                document.getElementById('dmAnalyzeBtn').disabled = false;
                return;   // job_id 保留在 sessionStorage，刷新后恢复轮询
            }
            setTimeout(() => pollJob(jobId), 2000);
        });
}

// 失败明细条：列出失败 mid_hash 与错误摘要；「重试失败项」把失败集合塞回 dmSelected 复用 startAnalysis
function renderJobFailures(errors) {
    const bar = document.getElementById('dmFailBar');
    document.getElementById('dmFailList').innerHTML = errors.map(e =>
        '<li><code>' + escHtml(e.mid_hash || '-') + '</code>：' + escHtml(e.error) + '</li>').join('');
    bar.style.display = 'block';
    dmUpdateAnalyzeStatus('分析完成，' + errors.length + ' 项失败');
    document.getElementById('dmRetryBtn').onclick = function() {
        const mids = errors.map(e => e.mid_hash).filter(Boolean);
        if (!mids.length) {   // 整体性失败（如登录态失效的全局终止）无 mid_hash，无单项可重试
            dmUpdateAnalyzeStatus('失败为整体性错误（如登录态失效），无单项可重试——请处理后重新勾选分析');
            bar.style.display = 'none';
            return;
        }
        bar.style.display = 'none';
        dmSelected.clear();
        mids.forEach(m => dmSelected.add(m));
        startAnalysis();
    };
    document.getElementById('dmReloadBtn').onclick = function() {
        saveViewState();
        location.reload();
    };
}

// 现场保存/恢复（sessionStorage，仅本次会话）：job 完成重载页面后回到原标签页/筛选/排序/分页/滚动位置
function saveViewState() {
    const state = {
        tab: document.querySelector('.tab-btn.active')?.dataset.tab || 'overview',
        userFilter: userState.filter,
        userKw: document.getElementById('userSearch')?.value || '',
        userSort: userState.sort,
        userPage: userState.page,
        scrollY: window.scrollY,
    };
    sessionStorage.setItem('viewState_' + BVID, JSON.stringify(state));
}

function restoreViewState() {
    const raw = sessionStorage.getItem('viewState_' + BVID);
    if (!raw) return false;
    sessionStorage.removeItem('viewState_' + BVID);   // 一次性消费
    try {
        const s = JSON.parse(raw);
        userState.filter = s.userFilter || 'all';
        userState.kw = (s.userKw || '').trim().toLowerCase();
        userState.sort = s.userSort || 'risk';
        userState.page = s.userPage || 1;
        const searchEl = document.getElementById('userSearch');
        if (searchEl) searchEl.value = s.userKw || '';
        const sortEl = document.getElementById('userSort');
        if (sortEl) sortEl.value = userState.sort;
        document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.filter === userState.filter));
        renderUserCards();
        switchTab(s.tab || 'overview');
        window.scrollTo(0, s.scrollY || 0);
        return true;
    } catch (e) { return false; }
}

// 页面加载恢复：优先处理"分析完成重载"的现场恢复+结果提示+高亮；否则恢复手动刷新的现场；
// 再否则有未完成 job 则继续轮询
(function() {
    const flashMsg = sessionStorage.getItem('dmFlashMsg_' + BVID);
    if (flashMsg) {
        sessionStorage.removeItem('dmFlashMsg_' + BVID);
        const uids = JSON.parse(sessionStorage.getItem('dmFlash_' + BVID) || '[]');
        sessionStorage.removeItem('dmFlash_' + BVID);
        restoreViewState();            // 恢复重载前的标签页/筛选/排序/分页/滚动
        switchTab('users');
        dmUpdateAnalyzeStatus(flashMsg);
        uids.forEach(uid => {
            const el = document.getElementById('uid-' + uid);
            if (el) {
                el.classList.add('flash-highlight');
                setTimeout(() => el.classList.remove('flash-highlight'), 3000);
            }
        });
        // gotoUser 内部会把筛选重置为"全部"并翻到目标卡片所在页——新卡片可见优先于现场筛选
        if (uids.length) gotoUser(uids[0]);
        return;
    }
    if (restoreViewState()) return;    // 「刷新查看结果」按钮的手动重载现场
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) {
        // 恢复未完成 job 的轮询：同样禁用发起按钮，防止并发 job 覆盖 DM_JOB_KEY
        document.getElementById('dmAnalyzeBtn').disabled = true;
        pollJob(jobId);
    }
    restoreTabFromHash();              // 前面分支未 return 时按 URL hash 还原标签页（job 续轮为异步轮询，不冲突）
})();

// ===== 报告页：重新生成 / 删除（spec 9） =====
function reportRegen() {
    if (!confirm('重新生成 ' + BVID + ' 的报告？将清空该视频缓存并后台重跑完整分析流水线。')) return;
    const status = document.getElementById('reportJobStatus');
    status.textContent = '重新生成发起中…';
    fetch('/api/video/' + encodeURIComponent(BVID) + '/regenerate', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { status.textContent = j.error || '发起失败'; return; }
            status.textContent = '重新生成中（完整流水线，可能需要几分钟）…';
            reportPollRegen(j.job_id);
        })
        .catch(() => { status.textContent = '网络错误'; });
}

function reportPollRegen(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            const status = document.getElementById('reportJobStatus');
            if (j.error) { status.textContent = '任务状态查询失败: ' + j.error; return; }
            if (j.finished) {
                if (j.errors && j.errors.length) {
                    status.textContent = '重新生成失败: ' + j.errors[0].error;
                    return;
                }
                location.reload();
                return;
            }
            setTimeout(() => reportPollRegen(jobId), 3000);
        })
        .catch(() => setTimeout(() => reportPollRegen(jobId), 3000));
}

function reportDelete() {
    if (!confirm('删除 ' + BVID + ' 的全部分析数据？\n包括：弹幕、发送者、用户画像、LLM 缓存（含共享深掘缓存）、全局映射、导出文件。\n注意：若其他视频涉及相同用户，其报告将缺数据。此操作不可恢复。')) return;
    fetch('/api/video/' + encodeURIComponent(BVID) + '/delete', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { alert(j.error || '删除失败'); return; }
            location.href = '/';
        })
        .catch(() => alert('网络错误'));
}
