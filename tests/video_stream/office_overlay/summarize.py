"""Read preserved raw trials and generate the comparison for the lab notes."""
import argparse
import gzip
from html import escape
import json
from pathlib import Path


def load(paths):
    runs = {}
    failures = {}
    for path in paths:
        with gzip.open(path, 'rt', encoding='utf-8') as file:
            data = json.load(file)
        for label, result in data['runs'].items():
            if label in runs:
                raise ValueError(f'Duplicate trial {label}')
            runs[label] = result
        failures.update(data['failures'])
    return runs, failures


def row(label, run):
    m, inputs = run['overlay'], run['summary']
    begin = next(e['t_ms'] for e in run['viewer_events'] if e['kind']=='start')
    end = next(e['t_ms'] for e in run['viewer_events'] if e['kind']=='window_end')
    length = sum(w['bytes'] for w in run['media_writes'] if begin <= w['written_ms'] < end)

    def phase(name):
        stats = m[name]
        return [stats['best'], stats['first_18_ms'], stats['first_sustained_18_ms']]

    return dict(label=label, shift=run['shift'], warm_ms=run['route']['warm_state_ms']['p50'],
                source=m['source_exact'], startup=phase('startup'), stopped=phase('stopped'),
                media_kib=round(length/1024, 1), tile_kib=round(m['overlay_payload_bytes']/1024, 1),
                tile_applied=m['tile_records_applied'], old_after_ms=m['last_old_patch_clear_after_resume_ms'],
                resume_draw_ms=m['first_resume_draw_ms'], resume_old=m['resume_overlays_on_moving_frame'],
                input_counts=[inputs[k] for k in ('input_queued','input_accepted','input_mailbox_receipts','input_acked')],
                input_ack_p95=inputs['input_ack_ms'].get('p95'), first_draw_ms=m['first_draw_ms'])


def chart(rows):
    colors = {'avc':1, 'jpeg':2, 'jpeg-diverse':3}
    labels = {'avc':'H.264', 'jpeg':'JPEG', 'jpeg-diverse':'降权 JPEG'}
    max_kib = max(r['media_kib'] for r in rows)
    bars = []
    for r in rows:
        condition = r['label'].split('-', 2)
        label = escape(f'{condition[0]}M · {condition[1]} · {labels[condition[2]]}')
        css = f'--series:var(--chart-{colors[condition[2]]})'
        bars.append(f'''<div class="row" style="{css}">
          <span>{label}</span>
          <div class="bars"><b style="width:{r["stopped"][0]/18*100:.1f}%"></b></div><strong>{r["stopped"][0]}/18</strong>
          <div class="bars"><b style="width:{r["media_kib"]/max_kib*100:.1f}%"></b></div><strong>{r["media_kib"]:.0f}</strong>
        </div>''')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <style>
      body { margin:0; color:var(--foreground); font: 13px var(--font-sans); }
      h3 { margin: 0 0 8px; font-size: 16px; }
      .row { display:grid; align-items:center; grid-template-columns: 135px 1fr 40px 1fr 35px; gap:6px;
             min-height:26px; border-bottom:1px solid var(--border); }
      .head {font-weight:600}
      .bars { background:var(--secondary); border-radius:3px; height:10px; overflow:hidden }
      .bars b { display:block; background:var(--series); height:100%; }
      strong { text-align:right; font-variant-numeric:tabular-nums; }
      p { margin:8px 0 0; color:var(--muted-foreground); }
      @media(max-width:430px) { .row {grid-template-columns:114px 1fr 34px 1fr 30px;gap:4px;} }
    </style>
    <h3>停滚后的小字识别与媒体流量</h3>
    <div class="row head"><span>链路 · 种子 · 方案</span><span>最佳字段数</span><span></span>
    <span>媒体 KiB</span><span></span></div>''' + '\n'.join(bars) + '''
    <p>每轮 25 秒。最佳字段数取停滚后约 8 秒内的单次峰值。连续 5 秒全对的判定见实验记录。</p>
    </html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('--chart', type=Path)
    args = parser.parse_args()
    runs, failures = load(args.inputs)
    rows = [row(label, run) for label, run in sorted(runs.items())]
    for entry in rows:
        print(json.dumps(entry, ensure_ascii=False))
    if failures:
        print(json.dumps({'failures':failures}, ensure_ascii=False))
    if args.chart:
        args.chart.write_text(chart(rows))


if __name__ == '__main__':
    main()
