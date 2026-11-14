import { api } from '../api.js';
import { ENUMS, basisTone, batchTone, can } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, fmtNum, fmtTime, kv, table, toast } from '../ui.js';

const state = { siteId: '', status: '' };

function shardsTable(shards) {
  if (!shards || shards.length === 0) return '<p class="muted">本批还没有分片检查点。</p>';
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>序号</th><th>分片文件</th><th>压缩</th><th>状态</th><th>记录数</th><th>内容指纹（sha256 前 12 位）</th><th>错误</th></tr></thead>
    <tbody>${shards
      .map(
        (shard) => `<tr>
          <td class="num mono">${esc(shard.shard_order)}</td>
          <td class="url mono">${esc(shard.loc)}<div class="muted">${esc(shard.source_file)}</div></td>
          <td>${shard.compressed ? '<span class="badge info">gzip</span>' : '<span class="muted">明文</span>'}</td>
          <td><span class="badge ${shard.status === 'done' ? 'ok' : shard.status === 'failed' ? 'danger' : 'warn'}">${esc(ENUMS.shardStatus[shard.status] || shard.status)}</span></td>
          <td class="num">${esc(fmtNum(shard.record_count))}</td>
          <td class="mono muted">${esc((shard.content_sha256 || '—').slice(0, 12))}</td>
          <td class="muted">${esc(shard.error || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function basisBlock(records) {
  const counts = {};
  records.forEach((record) => {
    counts[record.decision_basis] = (counts[record.decision_basis] || 0) + 1;
  });
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return '<p class="muted">本批还没有合并记录。</p>';
  return `<div class="chip-row">${entries
    .map(
      ([basis, count]) =>
        `<span class="chip ${basisTone(basis)}">${esc(ENUMS.decisionBasis[basis] || basis)}<b>${esc(count)}</b></span>`,
    )
    .join('')}</div>`;
}

function sourceList(record) {
  const sources = record.sources || [];
  if (sources.length === 0) return '<p class="muted">无来源明细。</p>';
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>分片序号</th><th>来源文件</th><th>loc</th><th>lastmod</th><th>lastmod 合法性</th></tr></thead>
    <tbody>${sources
      .map(
        (source) => `<tr>
          <td class="num mono">${esc(source.shard_order)}</td>
          <td class="mono">${esc(source.source_file)}</td>
          <td class="url mono">${esc(source.loc)}</td>
          <td class="mono">${esc(source.lastmod || '—')}</td>
          <td>${source.lastmod_valid ? '<span class="badge ok">合法</span>' : '<span class="badge danger">无法解析</span>'}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function reviewBlock(detail, ctx, reload) {
  const open = (detail.records || []).filter((record) => record.needs_review && record.review_status === 'open');
  if (open.length === 0) {
    return '<p class="muted">本批没有待人工复核的冲突。</p>';
  }
  const canReview = can(ctx.user.role, 'defect:review');
  return open
    .map(
      (record) => `<article class="review-card" data-review-id="${esc(record.id)}">
        <div class="review-head">
          <b class="url mono">${esc(record.canonical_url)}</b>
          <span class="badge danger">${esc(ENUMS.reviewStatus.open)}</span>
        </div>
        <p class="muted" style="margin:6px 0">原因：${esc((record.review_reasons || []).join('、'))}；临时采用 lastmod：<span class="mono">${esc(record.lastmod || '—')}</span>（${esc(ENUMS.decisionBasis[record.decision_basis] || record.decision_basis)}）</p>
        ${sourceList(record)}
        ${
          canReview
            ? `<form class="review-form" style="display:flex;gap:8px;align-items:end;margin-top:8px;flex-wrap:wrap">
                <div class="field" style="flex:1;min-width:160px"><label>裁决 lastmod（YYYY-MM-DD）</label>
                  <input name="lastmod" value="${esc(record.reviewed_lastmod || '')}" placeholder="2026-10-01" required pattern="\\d{4}-\\d{2}-\\d{2}" /></div>
                <div class="field" style="flex:2;min-width:200px"><label>裁决依据备注</label><input name="note" placeholder="例如：以栏目详情页发布时间为准" /></div>
                <button class="btn accent sm" type="submit">提交裁决</button>
              </form>`
            : '<p class="muted">当前角色只读，待复核由巡检专员或主管裁决。</p>'
        }
      </article>`,
    )
    .join('');
}

function bindReviewForms(view, ctx, batchId, reload) {
  view.querySelectorAll('.review-form').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const card = form.closest('.review-card');
      const recordId = card.dataset.reviewId;
      const button = form.querySelector('button[type=submit]');
      button.disabled = true;
      try {
        await api.patch(`/api/sitemap-snapshots/reviews/${recordId}`, {
          lastmod: form.lastmod.value.trim(),
          note: form.note.value.trim() || null,
        });
        toast('裁决已保存并同步到当前快照主表');
        await reload();
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
      }
    });
  });
}

async function openBatch(view, ctx, batchId, reload) {
  let detail;
  try {
    detail = await api.get(`/api/sitemap-snapshots/${batchId}`);
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const canCrawl = can(ctx.user.role, 'crawl:create');
  const resumable = detail.status === 'expanding' || detail.status === 'failed';
  const { root, close } = drawer({
    title: `快照批次 ${detail.batch_no}`,
    subtitle: `${esc(detail.site_name || detail.site_code)} · ${detail.index_url}`,
    body: `${kv([
      ['完成状态', `<span class="badge ${batchTone(detail.status)}">${esc(detail.status_label)}</span>`],
      ['本批分片', `<span class="mono">${esc(detail.shard_done)} / ${esc(detail.shard_total)} 完成${detail.shard_failed ? `，失败 ${esc(detail.shard_failed)}` : ''}</span>`],
      ['原始记录 / canonical', `<span class="mono">${esc(fmtNum(detail.record_total))} / ${esc(fmtNum(detail.canonical_total))}</span>`],
      ['冲突数', `<span class="mono">${esc(fmtNum(detail.conflict_total))}</span>（同一 canonical 跨分片重复且 lastmod 不一致）`],
      ['待复核', `<span class="mono">${esc(fmtNum(detail.review_open_total))} / ${esc(fmtNum(detail.needs_review_total))} 未裁决</span>`],
      ['快照指纹', `<span class="mono">${esc((detail.snapshot_fingerprint || '—').slice(0, 24))}…</span>`],
      ['顶替批次', detail.superseded_batch_id ? `<span class="mono">#${esc(detail.superseded_batch_id)}</span>` : '—'],
      ['最近错误', esc(detail.last_error || '—')],
      ['开始 / 完成', `${fmtTime(detail.started_at)} / ${fmtTime(detail.finished_at)}`],
      ['激活时间', fmtTime(detail.activated_at)],
    ])}
    <div class="rule-label">本批分片（断点续跑只重读失败分片，已完成不重复）</div>
    ${shardsTable(detail.shards)}
    <div class="rule-label">采用依据分布</div>
    ${basisBlock(detail.records)}
    <div class="rule-label">待人工复核（${esc(detail.review_open_total)}）</div>
    <div id="review-list">${reviewBlock(detail, ctx, reload)}</div>`,
    footer: resumable && canCrawl ? '<button class="btn accent" data-resume>断点续跑该批次</button>' : '<span class="mono muted">批次已结束，无需续跑</span>',
  });
  bindReviewForms(root, ctx, batchId, async () => {
    close();
    await reload();
  });
  root.querySelector('[data-resume]')?.addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      const updated = await api.post(`/api/sitemap-snapshots/${batchId}/resume`, {});
      toast(`批次 ${updated.batch_no} 续跑完成：${updated.status_label}`);
      close();
      await reload();
    } catch (err) {
      toast(errorMessage(err), 'err');
      event.target.disabled = false;
    }
  });
}

function openCreate(view, ctx, sites, reload) {
  const options = sites.map((site) => ({ value: site.id, label: `${site.name}（${site.code}）` }));
  const { root, close } = drawer({
    title: '展开 sitemap 索引快照',
    subtitle: '逐层读同站分片（含 gzip），按规则合并 lastmod 冲突；成功后同事务激活，失败保留旧版',
    body: `<form id="snapshot-form" style="display:grid;gap:12px">
      ${field({ label: '目标站点', name: 'site_id', options })}
      <div class="mono muted">合并规则：① 单一来源直接采用；② lastmod 取最新；③ 最新并列取分片序号最小；④ 再并列取 loc 字典序最小；⑤ lastmod 无法解析进待复核，仍给确定性临时值。</div>
      <div class="form-error" id="snapshot-error"></div>
      <button class="btn accent" type="submit">展开并激活</button>
    </form>`,
  });
  root.querySelector('#snapshot-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.textContent = '展开中…';
    try {
      const detail = await api.post('/api/sitemap-snapshots', { site_id: Number(form.site_id.value) });
      if (detail.status === 'active') {
        toast(`快照 ${detail.batch_no} 已激活：分片 ${detail.shard_total}、冲突 ${detail.conflict_total}、待复核 ${detail.review_open_total}`);
      } else {
        toast(`快照 ${detail.batch_no} 状态 ${detail.status_label}，可在详情中断点续跑`, 'err');
      }
      close();
      await reload();
    } catch (err) {
      root.querySelector('#snapshot-error').textContent = errorMessage(err);
      button.disabled = false;
      button.textContent = '展开并激活';
    }
  });
}

export const snapshotsPage = {
  async render(view, ctx) {
    let batches;
    let sites;
    try {
      [batches, sites] = await Promise.all([
        api.get('/api/sitemap-snapshots', { site_id: state.siteId, status: state.status, limit: 50 }),
        api.get('/api/sites', { limit: 50 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => snapshotsPage.render(view, ctx));
      return;
    }
    const reload = () => snapshotsPage.render(view, ctx);
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>sitemap 索引快照</h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${esc(batches.total)} 个批次</span>
            ${can(ctx.user.role, 'crawl:create') ? '<button class="btn sm accent" id="new-snapshot">展开新快照</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${filterBar({
            id: 'snapshot-filters',
            fields: [
              { label: '站点', name: 'site_id', value: state.siteId, options: [{ value: '', label: '全部' }, ...sites.items.map((site) => ({ value: site.id, label: site.code }))] },
              { label: '状态', name: 'status', value: state.status, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.batchStatus).map(([value, label]) => ({ value, label }))] },
            ],
          })}
          <p class="mono muted" style="margin:8px 0 0">巡检的分页采集、分片合并、可达探测与缺陷对账全程绑定同一快照指纹；半批结果不进覆盖率与导出，激活失败自动恢复旧版。</p>
        </div>
        ${table({
          rows: batches.items,
          empty: { title: '还没有快照批次', hint: '发起一次快照展开，或直接发起巡检（会自动确保快照）。' },
          columns: [
            { key: 'batch_no', label: '批次号', className: 'mono' },
            { key: 'site_code', label: '站点', className: 'mono' },
            { key: 'status', label: '完成状态', render: (row) => `<span class="badge ${batchTone(row.status)}">${esc(row.status_label)}</span>` },
            { key: 'shard_done', label: '本批分片', className: 'num', render: (row) => `${esc(row.shard_done)}/${esc(row.shard_total)}${row.shard_failed ? ` <span class="badge danger">${esc(row.shard_failed)} 失败</span>` : ''}` },
            { key: 'canonical_total', label: 'canonical', className: 'num' },
            { key: 'conflict_total', label: '冲突数', className: 'num' },
            { key: 'review_open_total', label: '待复核', className: 'num', render: (row) => (row.review_open_total > 0 ? `<span class="badge danger">${esc(row.review_open_total)}</span>` : '0') },
            { key: 'snapshot_fingerprint', label: '指纹', className: 'mono', render: (row) => esc((row.snapshot_fingerprint || '—').slice(0, 10)) },
            { key: 'finished_at', label: '完成时间', render: (row) => fmtTime(row.finished_at) },
          ],
        })}
      </section>`;

    const filters = view.querySelector('#snapshot-filters');
    filters?.addEventListener('change', () => {
      state.siteId = filters.querySelector('[name=site_id]').value;
      state.status = filters.querySelector('[name=status]').value;
      reload();
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const batch = batches.items.find((item) => String(item.id) === row.dataset.id);
        if (batch) openBatch(view, ctx, batch.id, reload);
      });
    });
    view.querySelector('#new-snapshot')?.addEventListener('click', () => openCreate(view, ctx, sites.items, reload));
  },
};

export default snapshotsPage;
