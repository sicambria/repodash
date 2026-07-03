#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// world-class-inventory — cross-repo /1000 scorecard for every clone under ~/git.
// Reproducible automation of docs: world-class-inventory-standard.md.
// Zero external deps. Writes ~/git/INDEX.MD and ~/git/inventory.json.

import fs from 'node:fs';
import path from 'node:path';
import cp from 'node:child_process';
import os from 'node:os';

const ROOT = process.env.REPODASH_DIR || path.join(os.homedir(), 'git');
const SONAR_URL = process.env.SONAR_URL || 'http://localhost:9000';
const SCHEMA_VERSION = 1;

// ── tiny fs/git helpers ──────────────────────────────────────────────────────
const exists = (p) => { try { fs.accessSync(p); return true; } catch { return false; } };
const isDir = (p) => { try { return fs.statSync(p).isDirectory(); } catch { return false; } };
const sizeOf = (p) => { try { return fs.statSync(p).size; } catch { return 0; } };
const readSafe = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch { return ''; } };
const ls = (p) => { try { return fs.readdirSync(p); } catch { return []; } };

function git(repo, args) {
  try { return cp.execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); }
  catch { return ''; }
}

const EXCLUDE = new Set(['node_modules', '.git', 'dist', 'build', '.next', 'coverage',
  '__pycache__', '.venv', 'venv', '.cache', 'vendor', 'test-results', 'playwright-report',
  '.turbo', 'generated', '.serena', '.scannerwork']);

// bounded recursive walk returning relative file paths
function walk(dir, { maxDepth = 4 } = {}) {
  const out = [];
  (function rec(d, depth) {
    if (depth > maxDepth) return;
    for (const name of ls(d)) {
      if (EXCLUDE.has(name)) continue;
      const full = path.join(d, name);
      let st; try { st = fs.statSync(full); } catch { continue; }
      if (st.isDirectory()) rec(full, depth + 1);
      else out.push(path.relative(dir, full));
    }
  })(dir, 0);
  return out;
}

// ── env / sonar ──────────────────────────────────────────────────────────────
function envVal(repo, key) {
  const txt = readSafe(path.join(repo, '.env')) || readSafe(path.join(repo, '.env.local'));
  const m = txt.match(new RegExp('^' + key + '=(.*)$', 'm'));
  return m ? m[1].trim().replace(/^["']|["']$/g, '') : '';
}
function sonarProjectKey(repo) {
  const txt = readSafe(path.join(repo, 'sonar-project.properties'));
  const m = txt.match(/^sonar\.projectKey=(.*)$/m);
  return m ? m[1].trim() : '';
}
async function sonarMeasures(repo) {
  const key = sonarProjectKey(repo);
  if (!key) return null;
  const token = envVal(repo, 'SONAR_TOKEN');
  const host = envVal(repo, 'SONAR_HOST_URL') || SONAR_URL;
  const metrics = 'bugs,vulnerabilities,code_smells,security_hotspots,coverage,alert_status';
  const url = `${host}/api/measures/component?component=${encodeURIComponent(key)}&metricKeys=${metrics}`;
  const headers = {};
  if (token) headers.Authorization = 'Basic ' + Buffer.from(token + ':').toString('base64');
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 6000);
    const res = await fetch(url, { headers, signal: ctl.signal });
    clearTimeout(t);
    if (!res.ok) return { key, error: `http ${res.status}` };
    const j = await res.json();
    const out = { key };
    for (const m of (j.component?.measures || [])) out[m.metric] = m.value;
    return out;
  } catch (e) { return { key, error: 'unreachable' }; }
}

// ── repo discovery ───────────────────────────────────────────────────────────
function discover() {
  return ls(ROOT)
    .filter((n) => isDir(path.join(ROOT, n, '.git')))   // real clone, not a worktree gitfile
    .sort();
}

// ── type classification ──────────────────────────────────────────────────────
const CODE_EXT = new Set(['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.py', '.go', '.rs',
  '.rb', '.java', '.kt', '.swift', '.dart', '.php', '.c', '.cpp', '.h', '.sh', '.bash', '.vue', '.svelte']);
function isShebang(full) {
  try { const fd = fs.openSync(full, 'r'); const b = Buffer.alloc(2); fs.readSync(fd, b, 0, 2, 0); fs.closeSync(fd); return b.toString() === '#!'; }
  catch { return false; }
}
function classify(repo, files) {
  const has = (f) => exists(path.join(repo, f));
  if (has('package.json') || has('pyproject.toml') || has('setup.py') || has('go.mod') || has('Cargo.toml'))
    return 'code';
  // research takes priority over the source-count fallback: a manifest-less repo with a
  // paper/notebook signature is research even if it carries figure/build scripts.
  const tex = files.some((f) => f.endsWith('.tex'));
  const nb = files.filter((f) => f.endsWith('.ipynb')).length;
  const paperDir = isDir(path.join(repo, 'paper')) || isDir(path.join(repo, 'manuscript'));
  if (tex || nb >= 3 || paperDir) return 'research';
  // source files: known code extensions, plus extensionless shebang scripts (CLI tools)
  const extSrc = files.filter((f) => CODE_EXT.has(path.extname(f)));
  const shebangSrc = files.filter((f) => !path.extname(f) && f.split('/').length <= 3 && isShebang(path.join(repo, f)));
  const srcCount = extSrc.length + shebangSrc.length;
  // a tests/ dir with test scripts is a strong code signal even below the source threshold
  const hasTestDir = (isDir(path.join(repo, 'tests')) || isDir(path.join(repo, 'test')))
    && files.some((f) => /^tests?\/.*\.(sh|bash|py|js|mjs|ts)$/.test(f));
  if (srcCount >= 10 || (srcCount >= 4 && hasTestDir)) return 'code';
  return 'other';
}

// ── scoring primitives ───────────────────────────────────────────────────────
// each criterion pushes {id, max, credit(0..1)} into a dimension
function crit(id, max, credit) { return { id, max, credit: Math.max(0, Math.min(1, credit)) }; }
const band = (s) => s >= 800 ? 'World-class' : s >= 600 ? 'Quant-managed'
  : s >= 400 ? 'Defined' : s >= 200 ? 'Managed' : 'Ad hoc';

const CANONICAL_AUDITS = ['tooling', 'database', 'registration', 'route', 'cloudflare',
  'codebase-health', 'ai-framework-maturity', 'compliance'];

function scoreRepo(repo, files, sonar) {
  const p = (f) => path.join(repo, f);
  const has = (f) => exists(p(f));
  const dims = {};
  const D = (name) => (dims[name] = dims[name] || []);

  // fast lookups over walked file list
  const wfFiles = ls(p('.github/workflows')).filter((f) => /\.ya?ml$/.test(f));
  const huskyDir = p('.husky');
  const hookBodies = ['pre-commit', 'pre-push', 'pre-merge-commit']
    .map((h) => readSafe(path.join(huskyDir, h))).join('\n')
    + readSafe(p('.git/hooks/pre-commit'));
  const wfBodies = wfFiles.map((f) => readSafe(p(path.join('.github/workflows', f)))).join('\n');
  const dirtyCount = git(repo, ['status', '--porcelain']).split('\n').filter(Boolean).length;
  const commits = parseInt(git(repo, ['rev-list', '--count', 'HEAD']) || '0', 10);

  // ── U1 VCS hygiene (80)
  const readmeSize = ['README.md', 'README.public.md', 'README.txt', 'README'].reduce((m, f) => Math.max(m, sizeOf(p(f))), 0);
  D('U1 VCS hygiene').push(
    crit('U1.1 commits', 15, commits >= 1 ? 1 : 0),
    crit('U1.2 README', 20, readmeSize >= 400 ? 1 : readmeSize > 0 ? 0.5 : 0),
    crit('U1.3 LICENSE', 15, (has('LICENSE') || has('LICENSE.md') || has('LICENSE.txt')) ? 1 : 0),
    crit('U1.4 gitignore', 10, has('.gitignore') ? 1 : 0),
    crit('U1.5 clean tree', 20, dirtyCount < 50 ? 1 : dirtyCount < 200 ? 0.5 : 0),
  );

  // ── U2 Documentation (100)
  const docToc = has('docs/DOC_TOC.md') || has('docs/INDEX.md') || has('docs/index.md')
    || has('docs/README.md') || files.some((f) => /^docs\/.*(DOC_TOC|INDEX)\.md$/i.test(f));
  D('U2 Documentation').push(
    crit('U2.1 docs dir', 25, isDir(p('docs')) ? 1 : 0),
    crit('U2.2 agent guide', 25, (has('AGENTS.md') || has('CLAUDE.md')) ? 1 : 0),
    crit('U2.3 CONTRIBUTING', 20, has('CONTRIBUTING.md') ? 1 : 0),
    crit('U2.4 doc index', 30, docToc ? 1 : 0),
  );

  // ── U3 CI/CD (110)
  const secWf = /security|codeql|sonar|quality|audit/i.test(wfFiles.join(' ') + ' ' + wfBodies);
  D('U3 CI/CD').push(
    crit('U3.1 has CI', 40, wfFiles.length >= 1 ? 1 : 0),
    crit('U3.2 3+ workflows', 40, wfFiles.length >= 3 ? 1 : wfFiles.length >= 1 ? 0.5 : 0),
    crit('U3.3 security/quality wf', 30, secWf ? 1 : 0),
  );

  // ── U4 Guardrails / hooks (110)
  const preCommit = has('.husky/pre-commit') || has('.git/hooks/pre-commit');
  const prePush = has('.husky/pre-push');
  const hooksRun = /lint|test|typecheck|guard|check|sonar|secret|cleancode|prettier|ruff|pytest/i.test(hookBodies);
  D('U4 Guardrails').push(
    crit('U4.1 pre-commit', 40, preCommit ? 1 : 0),
    crit('U4.2 pre-push', 40, prePush ? 1 : 0),
    crit('U4.3 hooks run checks', 30, (preCommit || prePush) && hooksRun ? 1 : 0),
  );

  // ── U5 Roadmap & planning (50)
  const roadmap = has('ROADMAP.md') || has('TODO.md') || has('BACKLOG.md') || isDir(p('docs/roadmap'));
  const plans = isDir(p('docs/plans')) || isDir(p('docs/errors'));
  // count open "- [ ]" items across roadmap/backlog sources (matches repodash roadmap section)
  const roadmapFiles = ['ROADMAP.md', 'TODO.md', 'BACKLOG.md']
    .filter((f) => has(f)).map((f) => p(f))
    .concat(files.filter((f) => /^docs\/roadmap\/.*\.md$/.test(f)).map((f) => p(f)));
  let roadmapItems = 0;
  for (const f of roadmapFiles) {
    const mm = readSafe(f).match(/^[ \t]*[-*] \[ \]/gm);
    if (mm) roadmapItems += mm.length;
  }
  D('U5 Roadmap').push(
    crit('U5.1 roadmap', 25, roadmap ? 1 : 0),
    crit('U5.2 plans/RCA', 25, plans ? 1 : 0),
  );

  // ── U6 Standards governance (50)
  const stdCount = ls(p('docs/standards')).filter((f) => f.endsWith('.md')).length;
  D('U6 Standards').push(
    crit('U6.1 standards dir', 25, stdCount >= 1 ? 1 : 0),
    crit('U6.2 5+ standards', 25, stdCount >= 5 ? 1 : stdCount >= 1 ? 0.5 : 0),
  );

  const type = classify(repo, files);
  const codeApplicable = type === 'code';

  // ── CODE-SPECIFIC (only credited for code repos; else N/A) ──
  const m = sonar || {};
  const num = (k) => m[k] === undefined ? null : parseFloat(m[k]);
  const gateOK = m.alert_status === 'OK';
  const analysed = m.alert_status !== undefined || m.bugs !== undefined;
  const sonarOptout = has('.sonar-optout');
  const gateEnforced = /sonar/i.test(hookBodies) || (analysed && !sonarOptout);

  D('C1 Sonar onboarding').push(
    crit('C1.1 sonar config', 30, has('sonar-project.properties') ? 1 : 0),
    crit('C1.2 analysed', 30, analysed ? 1 : 0),
    crit('C1.3 gate OK', 30, gateOK ? 1 : 0),
    crit('C1.4 gate enforced', 20, has('sonar-project.properties') && gateEnforced ? 1 : 0),
  );

  const covVal = num('coverage');
  D('C2 Sonar posture').push(
    crit('C2.1 bugs=0', 25, num('bugs') === 0 ? 1 : 0),
    crit('C2.2 vulns=0', 25, num('vulnerabilities') === 0 ? 1 : 0),
    crit('C2.3 smells=0', 20, num('code_smells') === 0 ? 1 : 0),
    crit('C2.4 hotspots=0', 20, num('security_hotspots') === 0 ? 1 : 0),
    crit('C2.5 coverage', 40, covVal === null ? 0 : covVal >= 80 ? 1 : covVal >= 55 ? 0.5 : 0),
  );

  const pkg = readSafe(p('package.json'));
  const runnerConfig = /vitest|jest|playwright|"test"\s*:/.test(pkg)
    || has('vitest.config.ts') || has('jest.config.js') || has('playwright.config.ts')
    || has('pytest.ini') || has('tox.ini') || /pytest/.test(readSafe(p('pyproject.toml')));
  const testFiles = files.filter((f) => /(\.test\.|\.spec\.|(^|\/)test_.*\.py$|_test\.go$)/.test(f)).length;
  const covTool = /--coverage|c8|nyc|coverage/.test(pkg) || has('.coveragerc') || has('.nycrc')
    || /coverage/.test(readSafe(p('vitest.config.ts')));
  D('C3 Test infra').push(
    crit('C3.1 runner', 35, runnerConfig ? 1 : 0),
    crit('C3.2 test files', 40, testFiles >= 1 ? 1 : 0),
    crit('C3.3 coverage tooling', 35, covTool ? 1 : 0),
  );

  // audit coverage: scan docs/audits + docs/meta filenames for canonical audit names
  const auditFiles = files.filter((f) => /^docs\/(audits|meta)\//.test(f) && f.endsWith('.md'));
  const auditBlob = auditFiles.join('\n').toLowerCase();
  // boundary-aware so short keys don't false-match (e.g. "route" must not hit "router")
  const auditsRun = CANONICAL_AUDITS.filter((a) => new RegExp(`(^|[^a-z])${a}([^a-z]|$)`).test(auditBlob));
  D('C4 Audit coverage').push(
    crit('C4.1 1+ audit', 40, auditsRun.length >= 1 ? 1 : 0),
    crit('C4.2 4+ audits', 40, auditsRun.length >= 4 ? 1 : auditsRun.length >= 1 ? auditsRun.length / 8 : 0),
  );

  const lintCfg = has('eslint.config.mjs') || has('eslint.config.js') || has('.eslintrc')
    || has('.eslintrc.js') || has('.eslintrc.json') || has('.eslintrc.cjs')
    || has('ruff.toml') || has('.flake8') || /"ruff"|\[tool\.ruff\]/.test(readSafe(p('pyproject.toml')));
  const cleanCfg = has('.jscpd.json') || has('stryker.conf.json') || has('stryker.conf.js')
    || files.some((f) => /budgets.*baseline.*\.json$/.test(f));
  const lintEnforced = /\blint\b|eslint|ruff|prettier/i.test(hookBodies + '\n' + wfBodies);
  D('C5 Lint/clean-code').push(
    crit('C5.1 lint config', 25, lintCfg ? 1 : 0),
    crit('C5.2 clean-code config', 20, cleanCfg ? 1 : 0),
    crit('C5.3 lint enforced', 25, lintEnforced ? 1 : 0),
  );

  // ── roll up ──
  const codeDimNames = ['C1 Sonar onboarding', 'C2 Sonar posture', 'C3 Test infra', 'C4 Audit coverage', 'C5 Lint/clean-code'];
  let earned = 0, absMax = 0, applMax = 0;
  const dimScores = {};
  for (const [name, crits] of Object.entries(dims)) {
    const dEarned = crits.reduce((s, c) => s + c.credit * c.max, 0);
    const dMax = crits.reduce((s, c) => s + c.max, 0);
    const isCode = codeDimNames.includes(name);
    dimScores[name] = { earned: Math.round(dEarned), max: dMax, na: isCode && !codeApplicable };
    absMax += dMax;
    if (!isCode || codeApplicable) { earned += dEarned; applMax += dMax; }
    else { /* code dim, non-code repo: contributes 0 to earned, excluded from applMax */ }
  }
  const absolute = Math.round(earned);                       // out of 1000 fixed
  const adjusted = Math.round((earned / applMax) * 1000);    // out of applicable

  return {
    repo: path.basename(repo), type,
    absolute, adjusted, band: band(absolute), adjustedBand: band(adjusted),
    sonar: sonar && sonar.key ? {
      key: sonar.key, gate: m.alert_status || null, error: m.error || null,
      bugs: num('bugs'), vulnerabilities: num('vulnerabilities'), code_smells: num('code_smells'),
      security_hotspots: num('security_hotspots'), coverage: covVal,
    } : null,
    signals: {
      commits, dirty: dirtyCount, workflows: wfFiles.length,
      hooks: ls(huskyDir).filter((f) => f !== '_' && !f.startsWith('.') && exists(path.join(huskyDir, f)) && fs.statSync(path.join(huskyDir, f)).isFile()).length,
      rubricHooks: [preCommit && 'pre-commit', prePush && 'pre-push'].filter(Boolean).length,
      standards: stdCount, auditFiles: auditFiles.length, auditsRun, roadmap, roadmapItems, plans,
      testFiles, readmeBytes: readmeSize,
    },
    dimensions: dimScores,
  };
}

// ── render ───────────────────────────────────────────────────────────────────
function esc(s) { return String(s).replace(/\|/g, '\\|'); }
function bar(score) {
  const n = Math.round(score / 100); return '█'.repeat(n) + '░'.repeat(10 - n);
}
function sonarCell(s) {
  if (!s) return '—';
  if (s.error) return `⚠️ ${s.error}`;
  const g = s.gate === 'OK' ? '✅' : s.gate ? '❌' : '—';
  const zero = (s.bugs === 0 && s.vulnerabilities === 0 && s.code_smells === 0 && s.security_hotspots === 0) ? '0-issue' : `${s.bugs}b/${s.vulnerabilities}v/${s.code_smells}s/${s.security_hotspots}h`;
  const cov = s.coverage === null ? '' : ` ${s.coverage}%cov`;
  return `${g} ${zero}${cov}`;
}

function render(rows, meta) {
  const byAbs = [...rows].sort((a, b) => b.absolute - a.absolute);
  const L = [];
  L.push('# ~/git Repository Inventory — World-Class Scorecard');
  L.push('');
  L.push(`> **Generated:** ${meta.date} · **Repos:** ${rows.length} · **Sonar:** ${meta.sonarUp ? 'up' : '⚠️ unreachable (code scores degraded)'} · **Schema:** v${SCHEMA_VERSION}`);
  L.push('>');
  L.push('> Reproducible automation of [`world-class-inventory-standard.md`](repodash/inventory/world-class-inventory-standard.md).');
  L.push('> Regenerate: `node ~/git/repodash/inventory/world-class-inventory.mjs`. **changemappers is the gold standard** (absolute ceiling).');
  L.push('');
  L.push('- **Absolute /1000** — raw score on the fixed 1000-point rubric; research/other repos read low *by design* (they cannot earn the 500-pt code block).');
  L.push('- **Adjusted /1000** — `earned ÷ applicable × 1000`; a repo scored world-class *for its type*.');
  L.push('- Bands: Ad hoc 0–199 · Managed 200–399 · Defined 400–599 · Quant-managed 600–799 · World-class 800–1000.');
  L.push('');
  L.push('## Scorecard');
  L.push('');
  L.push('| # | Repo | Type | Absolute | Adjusted | Band | Sonar | Guardrails | Workflows | Standards | Audits run | Roadmap items |');
  L.push('|--:|------|------|:--------:|:--------:|------|-------|:----------:|:---------:|:---------:|-----------|:-------:|');
  byAbs.forEach((r, i) => {
    L.push('| ' + [
      i + 1,
      `**${esc(r.repo)}**`,
      r.type,
      `${r.absolute} \`${bar(r.absolute)}\``,
      `${r.adjusted}`,
      r.band,
      sonarCell(r.sonar),
      `${r.signals.hooks} hooks`,
      r.signals.workflows,
      r.signals.standards,
      `${r.signals.auditsRun.length}/8${r.signals.auditFiles ? ` (${r.signals.auditFiles} rpt)` : ''}`,
      r.signals.roadmap ? `${r.signals.roadmapItems} open` : '—',
    ].join(' | ') + ' |');
  });
  L.push('');

  // type breakdown
  const groups = { code: [], research: [], other: [] };
  byAbs.forEach((r) => groups[r.type].push(r));
  L.push('## By type');
  L.push('');
  for (const t of ['code', 'research', 'other']) {
    if (!groups[t].length) continue;
    L.push(`**${t}** (${groups[t].length}): ` + groups[t].map((r) => `${r.repo} (${r.absolute}/${r.adjusted})`).join(', '));
    L.push('');
  }

  // audit matrix for code repos
  const AUD = ['tooling', 'database', 'registration', 'route', 'cloudflare', 'codebase-health', 'ai-framework-maturity', 'compliance'];
  const codeRepos = byAbs.filter((r) => r.type === 'code');
  L.push('## Audit-standard coverage (code repos)');
  L.push('');
  L.push('| Repo | ' + AUD.join(' | ') + ' |');
  L.push('|------|' + AUD.map(() => ':--:').join('|') + '|');
  codeRepos.forEach((r) => {
    L.push('| ' + esc(r.repo) + ' | ' + AUD.map((a) => r.signals.auditsRun.includes(a) ? '✅' : '·').join(' | ') + ' |');
  });
  L.push('');
  L.push('*Tracks only the **8 canonical changemappers audit-standards**. A `·` means that named standard has no audit report — **not** that the repo was never audited: ad-hoc/finding audits are counted as report totals (`N rpt`) in the scorecard\'s "Audits run" column.*');
  L.push('');

  // dimension detail (top repos)
  L.push('## Dimension detail');
  L.push('');
  const dimOrder = ['U1 VCS hygiene', 'U2 Documentation', 'U3 CI/CD', 'U4 Guardrails', 'U5 Roadmap', 'U6 Standards',
    'C1 Sonar onboarding', 'C2 Sonar posture', 'C3 Test infra', 'C4 Audit coverage', 'C5 Lint/clean-code'];
  const shortHdr = ['U1', 'U2', 'U3', 'U4', 'U5', 'U6', 'C1', 'C2', 'C3', 'C4', 'C5'];
  L.push('| Repo | ' + shortHdr.join(' | ') + ' |');
  L.push('|------|' + shortHdr.map(() => ':--:').join('|') + '|');
  byAbs.forEach((r) => {
    const cells = dimOrder.map((d) => {
      const dd = r.dimensions[d];
      if (!dd) return '·';
      if (dd.na) return 'n/a';
      return `${dd.earned}/${dd.max}`;
    });
    L.push('| ' + esc(r.repo) + ' | ' + cells.join(' | ') + ' |');
  });
  L.push('');
  L.push('*Dimension legend:* U1 VCS hygiene · U2 Docs · U3 CI/CD · U4 Guardrails · U5 Roadmap · U6 Standards · C1 Sonar onboarding · C2 Sonar posture · C3 Test infra · C4 Audit coverage · C5 Lint/clean-code (C* = n/a for non-code).');
  L.push('');
  L.push('---');
  L.push(`*Rendered from \`~/git/inventory.json\` by \`world-class-inventory.mjs\`. Do not hand-edit — re-run the script.*`);
  return L.join('\n') + '\n';
}

// ── main ─────────────────────────────────────────────────────────────────────
async function main() {
  let sonarUp = false;
  try {
    const res = await fetch(`${SONAR_URL}/api/system/status`, { signal: AbortSignal.timeout(4000) });
    sonarUp = res.ok;
  } catch { sonarUp = false; }

  const repos = discover();
  const rows = [];
  for (const name of repos) {
    const repo = path.join(ROOT, name);
    const files = walk(repo);
    const type = classify(repo, files);
    const sonar = (type === 'code' && exists(path.join(repo, 'sonar-project.properties'))) ? await sonarMeasures(repo) : null;
    rows.push(scoreRepo(repo, files, sonar));
    process.stderr.write(`  scored ${name}\n`);
  }

  const date = new Date().toISOString().slice(0, 10);
  const meta = { date, sonarUp, schema: SCHEMA_VERSION, root: ROOT };
  fs.writeFileSync(path.join(ROOT, 'inventory.json'), JSON.stringify({ meta, repos: rows }, null, 2));
  fs.writeFileSync(path.join(ROOT, 'INDEX.MD'), render(rows, meta));
  process.stderr.write(`\nWrote ${path.join(ROOT, 'INDEX.MD')} and inventory.json (${rows.length} repos)\n`);
}

main().catch((e) => { console.error(e); process.exit(1); });
