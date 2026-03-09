#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
登記所備付地図（法務省 tizuxml）→ SIMA（.sim）変換 GUI ツール
・出力を CRLF (Windows改行) に統一
・XMLの <座標系> と <変換プログラム> から自動抽出
・アイコン設定（icon.png / icon.ico を同梱していれば自動反映）
・ドラッグ＆ドロップ対応（tkinterdnd2 が有れば）
"""

import os
import sys
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import xml.etree.ElementTree as ET
import re
import unicodedata

# --- D&D (optional) ---
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    TkBase = TkinterDnD.Tk
    DND_AVAILABLE = True
except Exception:
    TkBase = tk.Tk
    DND_AVAILABLE = False

NS = {
    't': 'http://www.moj.go.jp/MINJI/tizuxml',
    'm': 'http://www.moj.go.jp/MINJI/tizumen',  # ← tizumen 名前空間（例: zmn:）
    'z': 'http://www.opengis.net/gml/2.1.2'
}
GML = '{*}'

# --- util: PyInstaller / dev 両対応のリソースパス ---
def res_path(rel: str) -> str:
    base = getattr(sys, '_MEIPASS', os.path.abspath('.'))
    return os.path.join(base, rel)


def sanitize_xml_text(text: str) -> str:
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', text)
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9A-Fa-f]+;)', '&amp;', text)
    text = ''.join(ch for ch in text if (unicodedata.category(ch) not in ('Cc', 'Cf') or ch in '\t\r\n'))
    text = text.replace('\x7f', '')
    return text


def _parse_with_diag(s: str):
    try:
        return ET.fromstring(s)
    except ET.ParseError as e:
        line, col = getattr(e, 'position', (None, None))
        if line:
            lines = s.splitlines()
            bad = lines[line-1] if 1 <= line <= len(lines) else ''
            snippet = bad[max(0, (col or 0)-30):(col or 0)+30]
            raise ET.ParseError(f"not well-formed at line {line}, col {col}. context: {snippet}") from e
        raise


def _load_xml_root(xml_path: str):
    with open(xml_path, 'rb') as f:
        data = f.read()
    i = data.find(b'<')
    if i > 0:
        data = data[i:]

    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        s = sanitize_xml_text(data.decode('utf-16', errors='ignore'))
        return _parse_with_diag(s)
    if data[:200].count(b'\x00') > 20:
        s = sanitize_xml_text(data.decode('utf-16', errors='ignore'))
        return _parse_with_diag(s)

    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass

    m = re.match(br'<\?xml[^>]*encoding=["\']([^"\']+)["\']', data[:200])
    encs = []
    if m:
        encs.append(m.group(1).decode('ascii', 'ignore').lower())
    encs += ['utf-8-sig', 'cp932', 'shift_jis', 'utf-8']

    for enc in encs:
        try:
            s = sanitize_xml_text(data.decode(enc, errors='ignore'))
            return _parse_with_diag(s)
        except Exception:
            continue

    s = sanitize_xml_text(data.decode('utf-8', errors='ignore'))
    return _parse_with_diag(s)


def parse_tizu_xml(xml_path):
    root = _load_xml_root(xml_path)

    def extract_cs_text():
        for tag in ['座標系', 't:座標系']:
            el = root.find(f'.//{tag}', NS) if ':' in tag else root.find(f'.//{tag}')
            if el is not None and el.text:
                return el.text.strip()
        return ''

    cs_text = extract_cs_text()
    datum_kind = '公共座標' if '公共' in cs_text else ('任意座標系' if '任意' in cs_text else '')
    m_zone = re.search(r'(\d+)\s*系', cs_text)
    zone_num = f"{m_zone.group(1)}系" if m_zone else ''

    # <変換プログラム>を抽出
    def extract_conv_prog():
        for tag in ['変換プログラム', 't:変換プログラム']:
            el = root.find(f'.//{tag}', NS) if ':' in tag else root.find(f'.//{tag}')
            if el is not None and el.text:
                return el.text.strip()
        return ''
    conv_prog = extract_conv_prog()

    # --- 追加: 市区町村名・大字名の抽出 ---
    def extract_city():
        for tag in ['市区町村名', 't:市区町村名']:
            el = root.find(f'.//{tag}', NS) if ':' in tag else root.find(f'.//{tag}')
            if el is not None and el.text:
                return el.text.strip()
        return ''

    def extract_oaza_list():
        names = []
        # まず全体から大字名を収集（筆の直下にある場合が多い）
        for tag in ['大字名', 't:大字名']:
            for el in (root.findall(f'.//{tag}', NS) if ':' in tag else root.findall(f'.//{tag}')):
                if el is not None and el.text:
                    name = el.text.strip()
                    if name and name not in names:
                        names.append(name)
        return names

    city_name = extract_city()
    oaza_names = extract_oaza_list()
    oaza_text = '、'.join(oaza_names) if oaza_names else ''

    # --- 高速化: parent_map と idref逆引きマップを同時構築 (O(N)、1回走査) ---
    parent_map: dict = {}
    idref_to_elems: dict[str, list] = {}   # idref値 → その属性を持つ要素リスト
    for p in root.iter():
        for c in p:
            parent_map[c] = p
            idr = c.get('idref')
            if idr:
                idref_to_elems.setdefault(idr, []).append(c)

    def _localname(tag: str) -> str:
        return tag.rsplit('}', 1)[-1] if '}' in tag else tag

    def _first_desc_text(elem: ET.Element, tags):
        wanted = set(tags) if isinstance(tags, (list, tuple)) else {tags}
        for e in elem.iter():
            if _localname(e.tag) in wanted and e.text and e.text.strip():
                return e.text.strip()
        return None

    def _extract_name_from_container(container: ET.Element):
        ln = _localname(container.tag)
        if ln == '基準点':
            nm = _first_desc_text(container, ['名称'])
            if nm:
                return nm
        if ln == '筆界点':
            nm = _first_desc_text(container, ['点番名'])
            if nm:
                return nm
            nm = _first_desc_text(container, ['点番号', '点符号'])
            if nm:
                return nm
        return None

    def find_point_name(pid: str, point_elem: ET.Element):
        # idref逆引きマップで直接O(1)ルックアップ（旧: root.iter()で全走査 = O(N)）
        for ref in idref_to_elems.get(pid, []):
            par = parent_map.get(ref)
            while par is not None:
                nm = _extract_name_from_container(par)
                if nm:
                    return nm
                par = parent_map.get(par)
        par = parent_map.get(point_elem)
        while par is not None:
            nm = _extract_name_from_container(par)
            if nm:
                return nm
            par = parent_map.get(par)
        return None

    points, pid_to_name = {}, {}

    for p in root.findall(f'.//{GML}GM_Point'):
        pid = p.get('id')
        if not pid:
            continue
        pos = p.find(f'.//{GML}DirectPosition')
        if pos is None:
            continue
        x_el = pos.find(f'.//{GML}X')
        y_el = pos.find(f'.//{GML}Y')
        if x_el is None or y_el is None or not (x_el.text and y_el.text):
            continue
        try:
            x = float(x_el.text)
            y = float(y_el.text)
        except ValueError:
            continue
        points[pid] = (x, y)
        nm = find_point_name(pid, p)
        if nm is None or nm == '':
            for tag in ['t:点番名','m:点番名','点番名','t:点名','m:点名','点名']:
                alt = p.find(tag, NS) if ':' in tag else p.find(tag)
                if alt is not None and alt.text and alt.text.strip():
                    nm = alt.text.strip(); break
        if nm:
            pid_to_name[pid] = nm.replace(',', '，').strip()

    curve_to_pids = {}
    for c in root.findall(f'.//{GML}GM_Curve'):
        cid = c.get('id')
        if not cid:
            continue
        refs = c.findall(f'.//{GML}GM_PointRef.point')
        pids = [e.get('idref') for e in refs if e.get('idref')]
        if pids:
            curve_to_pids[cid] = pids

    surface_to_ring = {}
    for s in root.findall(f'.//{GML}GM_Surface'):
        sid = s.get('id')
        if not sid:
            continue
        gens = s.findall(f'.//{GML}GM_SurfaceBoundary.exterior//{GML}GM_CompositeCurve.generator')
        gen_ids = [g.get('idref') for g in gens if g.get('idref')]
        ring = []
        for cid in gen_ids:
            seq = curve_to_pids.get(cid)
            if not seq:
                continue
            if not ring:
                ring.extend(seq)
            else:
                if ring[-1] == seq[0]:
                    ring.extend(seq[1:])
                elif ring[-1] == seq[-1]:
                    ring.extend(list(reversed(seq[:-1])))
                else:
                    ring.extend(seq)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        dedup = []
        for pid in ring:
            if not dedup or dedup[-1] != pid:
                dedup.append(pid)
        surface_to_ring[sid] = dedup

    parcels = []
    for h in list(root.findall('.//筆')) + list(root.findall('.//t:筆', NS)):
        chiban_el = h.find('地番') if h.find('地番') is not None else h.find('t:地番', NS)
        chiban = (chiban_el.text or '').strip() if chiban_el is not None else ''
        shape = h.find('形状') if h.find('形状') is not None else h.find('t:形状', NS)
        sid = shape.get('idref') if shape is not None else ''
        ring_ids = surface_to_ring.get(sid, [])
        parcels.append({'地番': chiban, 'surface_id': sid, 'ring_point_ids': ring_ids})

    return points, parcels, pid_to_name, datum_kind, zone_num, conv_prog, city_name, oaza_text


def write_sima(sim_path, points, parcels, xml_filename, pid_to_name=None, datum_kind='', zone_num='', conv_prog='', city_name='', oaza_text=''):
    pid_to_name = pid_to_name or {}
    EOL = "\r\n"

    def pid_key(pid):
        try:
            return int(pid[1:])
        except Exception:
            return pid

    sorted_pids = sorted(points.keys(), key=pid_key)
    idx_of, name_of = {}, {}

    for i, pid in enumerate(sorted_pids, 1):
        idx_of[pid] = i
        nm = pid_to_name.get(pid)
        if nm is None:
            try:
                nm = int(pid[1:])
            except Exception:
                nm = i
        name_of[pid] = nm

    filename_root = os.path.splitext(os.path.basename(xml_filename))[0]

    with open(sim_path, 'w', encoding='cp932', newline='') as f:
        f.write(f"G00,01,{filename_root}," + EOL)
        f.write("Z00,xml2sima," + EOL)
        f.write(f"Z00,データム： {datum_kind}," + EOL)
        f.write(f"Z00,座標系  ： {zone_num}," + EOL)
        if city_name:
            f.write(f"Z00,市町村名： {city_name}," + EOL)
        if oaza_text:
            f.write(f"Z00,大字名： {oaza_text}," + EOL)
        if conv_prog:
            f.write(f"Z00,変換プログラム  ：{conv_prog}," + EOL)

        f.write("A00," + EOL)
        for pid in sorted_pids:
            i = idx_of[pid]
            nm = name_of[pid]
            x, y = points[pid]
            f.write(f"A01,{i},{nm},{x:.3f},{y:.3f},0.000," + EOL)
        f.write("A99," + EOL)

        parcel_no = 0
        for parcel in parcels:
            ring = [p for p in parcel['ring_point_ids'] if p in points]
            if len(ring) < 3:
                continue
            parcel_no += 1
            chiban = parcel['地番'] or str(parcel_no)
            f.write(f"D00,{parcel_no},{chiban},1," + EOL)
            last = None
            for pid in ring:
                i = idx_of.get(pid)
                nm = name_of.get(pid)
                if i is None or nm is None:
                    continue
                if last == pid:
                    continue
                f.write(f"B01,{i},{nm}," + EOL)
                last = pid
            f.write("D99," + EOL)


class App(TkBase):
    def __init__(self):
        super().__init__()
        self.title('登記所備付地図 XML → SIMA 変換ツール')
        self.geometry('800x560')
        self.var_xml = tk.StringVar()
        self.var_sim = tk.StringVar()
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        # --- アイコン設定（png/ico どちらでも可） ---
        try:
            icon_file = None
            for cand in ('icon.ico', 'icon.png', 'クリップボード02.png'):
                p = res_path(cand)
                if os.path.exists(p):
                    icon_file = p; break
            if icon_file:
                try:
                    # png でもOK（Tk 8.6以降）
                    self.iconphoto(True, tk.PhotoImage(file=icon_file))
                except Exception:
                    pass
        except Exception:
            pass

        row = ttk.Frame(frm)
        row.pack(fill=tk.X, pady=5)
        ttk.Label(row, text='入力XML:').pack(side=tk.LEFT)
        ent_xml = ttk.Entry(row, textvariable=self.var_xml)
        ent_xml.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row, text='参照', command=self.browse_xml).pack(side=tk.LEFT)

        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X, pady=5)
        ttk.Label(row2, text='出力SIM:').pack(side=tk.LEFT)
        ent_sim = ttk.Entry(row2, textvariable=self.var_sim)
        ent_sim.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row2, text='参照', command=self.browse_sim).pack(side=tk.LEFT)

        # --- D&D 対応 ---
        if DND_AVAILABLE:
            ent_xml.drop_target_register(DND_FILES)
            ent_sim.drop_target_register(DND_FILES)
            self.drop_target_register(DND_FILES)
            ent_xml.dnd_bind('<<Drop>>', self.on_drop_xml)
            ent_sim.dnd_bind('<<Drop>>', self.on_drop_sim)
            self.dnd_bind('<<Drop>>', self.on_drop_xml)  # 画面全体にドロップでもOK

        ttk.Button(frm, text='変換する', command=self.convert).pack(pady=10)
        self.txt = tk.Text(frm, height=18)
        self.txt.pack(fill=tk.BOTH, expand=True)
        if not DND_AVAILABLE:
            self.log('ヒント: ドラッグ＆ドロップを使うには "pip install tkinterdnd2" を実行してください。\n')
        self.log('準備完了。XML を指定またはドロップして［変換する］を押してください。\n')

    # --- D&D ハンドラ ---
    def _extract_first_path(self, event_data: str) -> str:
        # {C:\\path with space\\file.xml} のような形式を正規化
        if not event_data:
            return ''
        items = event_data.strip().split()
        if not items:
            return ''
        raw = items[0]
        if raw.startswith('{') and raw.endswith('}'):
            raw = raw[1:-1]
        return raw

    def on_drop_xml(self, event):
        path = self._extract_first_path(event.data)
        if path and path.lower().endswith('.xml') and os.path.exists(path):
            self.var_xml.set(path)
            base = os.path.splitext(os.path.basename(path))[0] + '.sim'
            self.var_sim.set(os.path.join(os.path.dirname(path), base))
            self.log(f'ドロップ: {path}\n')
        else:
            self.log('XML 以外がドロップされました。\n')

    def on_drop_sim(self, event):
        path = self._extract_first_path(event.data)
        if path:
            if path.lower().endswith('.sim'):
                self.var_sim.set(path)
            else:
                # フォルダや別名が落ちてきたら、その配下に .sim 名で置く
                folder = path if os.path.isdir(path) else os.path.dirname(path)
                base_xml = os.path.splitext(os.path.basename(self.var_xml.get() or 'output'))[0]
                self.var_sim.set(os.path.join(folder, base_xml + '.sim'))
            self.log(f'出力先: {self.var_sim.get()}\n')

    # --- 通常の操作 ---
    def log(self, msg):
        self.txt.insert(tk.END, msg)
        self.txt.see(tk.END)

    def browse_xml(self):
        path = filedialog.askopenfilename(filetypes=[('XML', '*.xml'), ('All files', '*.*')])
        if path:
            self.var_xml.set(path)
            base = os.path.splitext(os.path.basename(path))[0] + '.sim'
            self.var_sim.set(os.path.join(os.path.dirname(path), base))

    def browse_sim(self):
        path = filedialog.asksaveasfilename(defaultextension='.sim', filetypes=[('SIMA', '*.sim')])
        if path:
            self.var_sim.set(path)

    def convert(self):
        xml_path = self.var_xml.get().strip()
        sim_path = self.var_sim.get().strip()
        if not xml_path:
            messagebox.showwarning('確認', '入力XMLを指定してください。')
            return
        if not sim_path:
            messagebox.showwarning('確認', '出力SIMの保存先を指定してください。')
            return
        try:
            self.log(f'XML 解析中: {xml_path}\n')
            points, parcels, pid_to_name, datum_kind, zone_num, conv_prog, city_name, oaza_text = parse_tizu_xml(xml_path)
            self.log(f'  点の数: {len(points)}\n')
            self.log(f'  画地の数: {len(parcels)}\n')
            self.log('SIMA 書き出し中...\n')
            write_sima(sim_path, points, parcels, xml_path, pid_to_name, datum_kind=datum_kind, zone_num=zone_num, conv_prog=conv_prog, city_name=city_name, oaza_text=oaza_text)
            self.log(f'完了: {sim_path}\n')
            messagebox.showinfo('完了', 'SIMA へ変換しました。')
        except Exception:
            tb = traceback.format_exc()
            self.log(tb + '\n')
            messagebox.showerror('エラー', '変換に失敗しました。ログを確認してください。')


if __name__ == '__main__':
    App().mainloop()