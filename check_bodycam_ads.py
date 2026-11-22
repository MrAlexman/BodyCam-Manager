#!/usr/bin/env python3
"""Локальная сверка состава установки Bodycam и полей ADS в JSON-экспортах FModel."""
import argparse
import codecs
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


BUILD = "26707677"
INTEREST = re.compile(r"DT_WeaponItems|(?:^|/)BAS_|GE_WeaponStat|GASEffects|AimIn|AdsSettings|AdsSpeed", re.I)
FIELDS = {"adsspeed", "sprintadsspeed", "blendtime"}
SUFFIXES = (".pak", ".sig", ".utoc", ".ucas")
MAX_CANDIDATES = 200
MAX_EXPORT_BYTES = 128 * 1024**2


def decode_file(path):
    data = path.read_bytes()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    if data[:200:2].count(0) > 12:
        return data.decode("utf-16-be")
    if data[1:200:2].count(0) > 12:
        return data.decode("utf-16-le")
    return data.decode("utf-8-sig")


def build_evidence(root):
    info = {"ожидаемый_buildid": BUILD, "buildid": None, "состояние": "нет подтверждения"}
    if root.parent.name.casefold() != "common" or root.parent.parent.name.casefold() != "steamapps":
        return info
    manifest = root.parent.parent / "appmanifest_2406770.acf"
    if not manifest.is_file():
        return info
    data = decode_file(manifest)
    def values(key):
        return re.findall(r'"' + key + r'"\s*"([^"]*)"', data, re.I)
    buildids = values("buildid")
    if (values("appid") != ["2406770"] or [v.casefold() for v in values("installdir")] != [root.name.casefold()]
            or len(buildids) != 1 or not buildids[0].isascii() or not buildids[0].isdigit()):
        info["состояние"] = "манифест Steam не сопоставлен с выбранной установкой"
        return info
    info.update(buildid=buildids[0], источник=manifest.name,
                состояние="совпадает с 0.9.1" if buildids[0] == BUILD else "другая сборка Steam")
    return info


def install_evidence(root):
    dirs = root / "Bodycam" / "Content" / "Paks"
    counts = {s: 0 for s in SUFFIXES}
    if dirs.is_dir():
        for _, _, files in os.walk(dirs):
            for name in files:
                ext = Path(name).suffix.lower()
                if ext in counts:
                    counts[ext] += 1
    manifest = root / "Manifest_UFSFiles_Win64.txt"
    matches = set()
    if manifest.is_file():
        for line in decode_file(manifest).splitlines():
            text = line.replace("\\", "/").strip()
            if INTEREST.search(text):
                hit = re.search(r'(?:Bodycam/(?:Content|Plugins)/|/Game/)[^\t"]+?\.(?:uasset|umap|uexp|ubulk)(?=\s|$|")', text, re.I)
                if hit:
                    matches.add(hit[0])
    return {"сборка_Steam": build_evidence(root), "каталог_Paks_найден": dirs.is_dir(),
            "количество_файлов_по_типу": counts, "UFS_манифест_найден": manifest.is_file(),
            "уникальных_путей_кандидатов": len(matches), "предел_показа": MAX_CANDIDATES,
            "пути_из_UFS": sorted(matches)[:MAX_CANDIDATES]}


def pointer(parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def leaves(node, parts=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, parts + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, parts + (index,))
    else:
        yield parts, node


def animation_leaves(node, parts=()):
    """Keep object references and small settings without copying sampled animation curves."""
    if isinstance(node, list) and (len(node) > 32 or (len(node) > 4 and all(isinstance(n, (float, int)) for n in node))):
        yield parts, {"число_элементов": len(node)}
    elif isinstance(node, (dict, list)):
        for key, child in (node.items() if isinstance(node, dict) else enumerate(node)):
            yield from animation_leaves(child, parts + (key,))
    else:
        yield parts, node


def export_fields(data):
    objects = data if isinstance(data, list) else [data]
    def recognized(obj):
        if not isinstance(obj, dict):
            return False
        return ("Type" in obj and "Name" in obj) or ("row" in obj and isinstance(obj.get("anim_set"), dict))
    if not any(recognized(o) for o in objects):
        return None
    fields = []
    def row(parts, value, group):
        fields.append({"путь_JSON_Pointer": pointer(parts), "значение": value, "вид": group})
    def visit(node, parts=()):
        if isinstance(node, list):
            for i, child in enumerate(node):
                visit(child, parts + (i,))
        elif isinstance(node, dict):
            is_modifier = any(str(k).casefold() in ("modifierop", "modifiermagnitude") for k in node)
            if is_modifier:
                values = list(leaves(node))
                if any("ads" in str(val).casefold() or "aimdown" in str(val).casefold() for _, val in values):
                    for trail, val in values:
                        row(parts + trail, val, "модификатор ADS")
                    return
            for key, value in node.items():
                k = key.casefold()
                path = parts + (key,)
                if k in ("itemclassid", "item_class") and items_export:
                    for trail, val in animation_leaves(value):
                        row(path + trail, val, "идентификатор предмета")
                elif k == "blendoption" and any(str(p).casefold() == "adsblend" for p in parts):
                    for trail, val in animation_leaves(value):
                        row(path + trail, val, "режим смешивания AdsBlend")
                elif k == "animationdata":
                    for trail, val in animation_leaves(value):
                        row(path + trail, val, "связь строки с AnimationData")
                elif k.startswith("aimin") or (k in FIELDS and any(str(p).casefold() == "adssettings" for p in parts)):
                    group = "ссылка или настройка AimIn" if k.startswith("aimin") else "поле AdsSettings"
                    for trail, val in animation_leaves(value):
                        row(path + trail, val, group)
                elif k in ("adsspeed", "sprintadsspeed") and not isinstance(value, (dict, list)):
                    row(path, value, "поле ADS вне AdsSettings")
                elif k in ("objectname", "objectpath", "assetpathname") and isinstance(value, str) and "aimin" in value.casefold():
                    row(path, value, "ссылка на ресурс AimIn")
                else:
                    visit(value, path)
    for index, obj in enumerate(objects):
        if recognized(obj):
            prefix = (index,) if isinstance(data, list) else ()
            items_export = "dt_weaponitems" in str(obj.get("Name", "")).casefold() or "row" in obj
            before = len(fields)
            visit(obj, prefix)
            if len(fields) != before:
                for k in ("Name", "Type", "row"):
                    if isinstance(obj.get(k), str):
                        row(prefix + (k,), obj[k], "идентификатор экспорта")
    return fields


def scan_exports(folder):
    stats = {"прочитано_JSON_FModel": 0, "пропущено_других_JSON": 0, "ошибки": [], "совпадения": {}}
    for path in sorted(folder.rglob("*")):
        if path.suffix.casefold() != ".json" or path.is_symlink() or not path.is_file():
            continue
        name = path.relative_to(folder).as_posix()
        try:
            if path.stat().st_size > MAX_EXPORT_BYTES:
                raise ValueError("размер превышает 128 МиБ")
            fields = export_fields(json.loads(decode_file(path)))
        except (ValueError, OSError, RecursionError) as exc:
            stats["ошибки"].append({"файл": name, "причина": str(exc)})
            continue
        if fields is None:
            stats["пропущено_других_JSON"] += 1
        else:
            stats["прочитано_JSON_FModel"] += 1
            if fields:
                stats["совпадения"][name] = fields
    return stats


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", nargs="?", type=Path, help="корень игры с Bodycam.exe")
    parser.add_argument("--exports", type=Path, help="отдельная папка экспортированных JSON FModel")
    parser.add_argument("--out", type=Path, help="новый файл отчёта за пределами установки")
    args = parser.parse_args()
    gui = None
    if args.game is None:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        gui = tk.Tk()
        gui.withdraw()
        selected = filedialog.askdirectory(title="Выберите папку игры с Bodycam.exe", parent=gui)
        if not selected:
            return
        args.game = Path(selected)
        if not args.exports and messagebox.askyesno("Экспорты", "Выбрать отдельную папку с JSON FModel?", parent=gui):
            selected = filedialog.askdirectory(title="Папка экспортов JSON", parent=gui)
            args.exports = Path(selected) if selected else None
    root = args.game.resolve()
    if not ((root / "Bodycam.exe").is_file() or (root / "Bodycam/Content/Paks").is_dir()
            or (root / "Manifest_UFSFiles_Win64.txt").is_file()):
        parser.error("в папке не найдены признаки установки Bodycam")
    out_dir = Path(__file__).resolve().parent
    if out_dir.is_relative_to(root):
        out_dir = Path(tempfile.gettempdir()).resolve()
    out = (args.out or out_dir / f"bodycam-ads-{uuid4().hex[:8]}.json").resolve()
    if out.is_relative_to(root):
        parser.error("выберите --out вне папки установки")
    report = install_evidence(root)
    if args.exports:
        folder = args.exports.resolve()
        if not folder.is_dir() or root.is_relative_to(folder) or folder.is_relative_to(root):
            parser.error("--exports должна указывать на отдельную папку с экспортированными JSON вне установки")
        report["экспорты"] = scan_exports(folder)
    report["проверено_UTC"] = datetime.now(timezone.utc).isoformat()
    report["смысл"] = "BuildID относится к установке. Версию экспортов проверяйте отдельно. Поля и ссылки не измеряют фактическую длительность перехода ADS."
    with out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(out)
    if gui:
        messagebox.showinfo("Готово", f"Отчёт сохранён\n{out}", parent=gui)
        gui.destroy()


if __name__ == "__main__":
    try:
        run()
    except (OSError, ValueError, ImportError) as error:
        sys.exit(f"Не удалось выполнить проверку: {error}")
