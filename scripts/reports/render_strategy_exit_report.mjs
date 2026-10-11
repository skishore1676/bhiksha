// Stage this file beside the supplied artifact-tool node_modules symlink.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error('Usage: node renderer.mjs report.json output.xlsx');
const d = JSON.parse(await fs.readFile(inputPath, 'utf8'));
const w = Workbook.create();
const summary = w.worksheets.add('Strategy review');
const trades = w.worksheets.add('Trades');
const exits = w.worksheets.add('Exit detail');
const signals = w.worksheets.add('Signal detail');
const outDir = path.dirname(outputPath);
await fs.mkdir(outDir, {recursive: true});
const pretty = s => String(s ?? 'Unknown').replaceAll('_', ' ');
const available = x => x ?? 'Unknown';
const col = i => { let s = ''; for (i++; i; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + (i - 1) % 26) + s; return s; };
function format(sheet, title, headers, rows, widths) {
  sheet.showGridLines = false;
  const header = sheet === summary ? 1 : 6, first = header + 1;
  if (sheet !== summary) {
    sheet.getRange('A2').values = [[title]];
    sheet.getRange('A2').format.font = {name: 'Arial', size: 15, bold: true};
  }
  const last = col(headers.length - 1), end = Math.max(first, rows.length + header);
  sheet.getRange(`A${header}:${last}${header}`).values = [headers];
  if (rows.length) sheet.getRange(`A${first}:${last}${end}`).values = rows;
  sheet.getRange(`A${header}:${last}${end}`).format.font = {name: 'Arial', size: 10};
  sheet.getRange(`A${header}:${last}${header}`).format = {fill: '#253858', font: {name: 'Arial', size: 10, bold: true, color: '#FFFFFF'},
    wrapText: true, rowHeight: 56, horizontalAlignment: 'center', verticalAlignment: 'center'};
  sheet.getRange(`A${first}:${last}${end}`).format.rowHeight = 38;
  sheet.getRange(`A${first}:${last}${end}`).format.verticalAlignment = 'center';
  sheet.getRange(`A${first}:${last}${end}`).format.wrapText = true;
  widths.forEach((width, i) => {sheet.getRange(`${col(i)}${header}:${col(i)}${end}`).format.columnWidth = width;});
  if (rows.length) {
    const table = sheet.tables.add(`A${header}:${last}${end}`, true, sheet.name.replaceAll(' ', '') + 'Table');
    table.style = 'TableStyleLight1';
    table.showFilterButton = true;
  }
  sheet.freezePanes.freezeRows(header);
  sheet.freezePanes.freezeColumns(sheet === summary ? 4 : 2);
  return end;
}

const tradeHeaders = ['Group ID','Trade ID','Confirmed entry\n1 = yes','Lane','Underlying','Entry time CT','Open\n1 = yes',
  'P&L eligible\n1 = yes','Recorded P&L USD','Entry price','Exit price','Original quantity','Exit policy','Exit attribution',
  'Deployment','Plan revision','Option contract'];
const tradeRows = d.trades.map(t => [t.row_id,t.trade_id,Number(t.fill_confirmed),t.lane,t.symbol,
  t.entry_timestamp ? new Date(t.entry_timestamp) : null,Number(t.is_open),Number(t.pnl_eligible),available(t.realized_pnl_usd),
  t.entry_price,t.exit_price,t.original_entry_qty,t.exit_policy_id,t.exit_attribution,t.deployment_id,t.plan_revision_id,t.option_symbol]);
const tradeEnd = format(trades,'Recorded trade outcomes',tradeHeaders,tradeRows,[24,38,14,12,12,22,10,12,18,14,14,12,30,32,48,50,28]);
trades.getRange('A3').values = [[`Source: ${d.source_receipt.trading_db}; snapshot max event ${d.source_receipt.max_event_id}`]];
trades.getRange('I7:K'+tradeEnd).setNumberFormat('"$"#,##0.00;("$"#,##0.00)');
// Display time in UTC explicitly: dates are typed, and Excel has no timezone semantics.
trades.getRange('F6').values = [['Entry time UTC']];
trades.getRange('F7:F'+tradeEnd).setNumberFormat('mm/dd/yy hh:mm');
trades.getRange('A4').values = [['Live = recorded broker lane. Shadow = recorded modeled lane. Fees assumed $0. Open outcomes are not full closed-trade P&L.']];

const exitRows = d.exits.map(e => [e.row_id,e.trade_id,pretty(e.policy),e.clean,
  available(e.pnl_usd),e.evidence_status,e.exit_at ? new Date(e.exit_at) : null,e.baseline,e.experiment_hash,e.limitation || 'None']);
const exitEnd = format(exits,'Modeled exit evidence', ['Group ID','Trade ID','Exit policy','Clean result','Modeled P&L USD','Evidence status','Exit time UTC','Comparison baseline','Frozen experiment','Limitation'],exitRows,[24,38,32,14,18,32,22,32,56,60]);
exits.getRange('A3').values = [[`Source: ${d.source_receipt.exit_db}; application named-exit replay evaluator`]];
exits.getRange('A4').values = [['Model: next fresh executable bid after trigger and configured latency. Fees $0. Gaps and unfinished exits remain unavailable.']];
exits.getRange('E7:E'+exitEnd).setNumberFormat('"$"#,##0.00;("$"#,##0.00)');
exits.getRange('G7:G'+exitEnd).setNumberFormat('mm/dd/yy hh:mm');

const signalRows = d.signals.map(s => [s.row_id,s.signal_id,s.observed_at ? new Date(s.observed_at) : null,
  s.captured,s.outcome || 'Unresolved',s.trade_id || 'None']);
const signalEnd = format(signals,'Recorded signal funnel', ['Group ID','Signal ID','Signal time UTC','Captured','Last outcome','Trade ID'],signalRows,[24,75,22,14,32,38]);
signals.getRange('A3').values = [[`Source: trading events through event ${d.source_receipt.max_event_id}. Deduplicated by deployment, revision and signal ID.`]];
signals.getRange('A4').values = [['Captured = signal_decision or signal_outcome persisted. Fired = unique positive signal receipts, not guaranteed upstream totals.']];
signals.getRange('C7:C'+signalEnd).setNumberFormat('mm/dd/yy hh:mm');

const headers = ['Date','Week starting','Strategy','Source','Underlying','Lane','Direction','Version','Entry profile','Default exit',
  'Signals fired\nrecorded','Signals\ncaptured','Filled\ntrades','Closed\ntrades','Observed\nsessions',
  'Live recorded\nP&L USD','Shadow recorded\nP&L USD','Common clean\ncomparisons',
  ...d.exit_columns.map(p => `${pretty(p)}\nModeled USD`),
  'Coverage','Record ID','Inference author','Inference'];
const reviewed = [...d.rows].sort((a,b) => b.date.localeCompare(a.date) || b.common_comparisons-a.common_comparisons || b.filled-a.filled || (b.signals_fired||0)-(a.signals_fired||0) || a.strategy.localeCompare(b.strategy));
const rows = reviewed.map(r => [new Date(r.date+'T00:00:00Z'),new Date(r.week_start+'T00:00:00Z'),pretty(r.strategy),pretty(r.source),r.symbol,r.lane,r.direction,r.version,pretty(r.entry_profile),pretty(r.default_exit),
  available(r.signals_fired),available(r.signals_captured),r.filled,r.closed,r.observed_sessions,
  r.lane === 'live' ? available(r.recorded_pnl_usd) : 'n.a.',r.lane === 'shadow' ? available(r.recorded_pnl_usd) : 'n.a.',r.common_comparisons,
  ...d.exit_columns.map(p => available(r.modeled_pnl[p])),r.coverage,r.row_id,r.inference?.author || 'Not written',
  r.inference ? `Assessment: ${r.inference.assessment}.\nReason: ${r.inference.reason}\nNext step: ${r.inference.next_step}` : 'Inference not written.']);
const widths = [14,16,38,24,12,12,12,10,18,30,13,13,12,12,13,18,18,15,...d.exit_columns.map(()=>22),14,38,16,75];
const summaryEnd = format(summary,'Bhiksha strategy and exit review',headers,rows,widths);
summary.getRange(`A2:B${summaryEnd}`).setNumberFormat('mm/dd/yy');
summary.tabColor = '#253858';
trades.getRange('A5').values = [[`Report ${d.window.start} to ${d.window.end} (Central). Date = signal/entry cohort; week starts Monday. Outcomes through window end. Counts repeat across frozen exit sets; do not sum matrix context.`]];
summary.getRange(`P2:Q${summaryEnd}`).setNumberFormat('"$"#,##0;("$"#,##0)');
if (d.exit_columns.length) summary.getRange(`S2:${col(17 + d.exit_columns.length)}${summaryEnd}`).setNumberFormat('"$"#,##0;("$"#,##0)');
const lastCol = col(headers.length-1);
summary.getRange(`${lastCol}2:${lastCol}${summaryEnd}`).format.rowHeight = 78;
summary.getRange(`${lastCol}2:${lastCol}${summaryEnd}`).format.verticalAlignment = 'top';
summary.getRange(`${lastCol}2:${lastCol}${summaryEnd}`).conditionalFormats.add('containsText', {text: 'Assessment: evidence blocked',format:{fill:'#FFF2CC'}});
summary.getRange(`${lastCol}2:${lastCol}${summaryEnd}`).conditionalFormats.add('containsText', {text: 'Assessment: collecting',format:{fill:'#E8EEF6'}});
w.recalculate();
const confirmed = d.trades.filter(t=>t.fill_confirmed);
assert.deepEqual(summary.getRange(`A1:${lastCol}1`).values[0], headers);
if (rows.length) assert.equal(summary.getRange(`C2:C${summaryEnd}`).values.filter(r => r[0] != null).length, rows.length);
const inspection = await w.inspect({kind:'table',range:'Strategy review!A1:F3',include:'values,formulas',tableMaxRows:3,tableMaxCols:6,maxChars:2000});
const errorScan = await w.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:30},maxChars:2000});
await fs.writeFile(path.join(outDir,'excel-verification.json'),JSON.stringify({inspection:inspection.ndjson,errorScan:errorScan.ndjson,rows:rows.length,confirmedTrades:confirmed.length},null,2));
for (const [sheet,range,name] of [[summary,'A1:M5','review-identity'],[summary,`N1:${lastCol}5`,'review-results'],[trades,'A2:N12','trades'],[exits,'A2:J12','exits'],[signals,'A2:F12','signals']]) {
  const png = await w.render({sheetName:sheet.name,range,scale:1.3,format:'png'});
  await fs.writeFile(path.join(outDir,`${name}.png`),new Uint8Array(await png.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(w);
await xlsx.save(outputPath);
execFileSync('python3', [fileURLToPath(new URL('./hide_supporting_sheets.py', import.meta.url)), outputPath]);
console.log(JSON.stringify({status:'ok',output:outputPath,rows:rows.length,confirmedTrades:confirmed.length}));
