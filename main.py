#!/usr/bin/env python3
"""Локальный конфигуратор для игры Bodycam. Справочник читается рядом с программой."""


_RELEASE_FINGERPRINT = "8337050a81c6f9a83cfe21185c0bb130bdc454df99d9b54d3c306883ea0afef5"


# Каталог, каноническая идентичность ревизии и локальное хранилище.
import copy
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import uuid
import zlib
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from contextlib import contextmanager


def _catalog_error(path, message):
    raise ValueError(f"{path}: {message}")


def _parse_decimal(text):
    number = Decimal(text)
    approx = float(number)
    if math.isfinite(approx) and Decimal(str(approx)) == number:
        return approx
    return number


def _catalog_decode(data):
    """Recover the complete path even for duplicate keys in nested arrays."""
    invalid = object()
    seen_invalid = False

    def pairs(items):
        nonlocal seen_invalid
        obj = dict(items)
        if len(obj) != len(items):
            keys = set()
            for key, _ in items:
                if key in keys:
                    obj[invalid] = key
                    seen_invalid = True
                    break
                keys.add(key)
        return obj

    def constant(_):
        nonlocal seen_invalid
        seen_invalid = True
        return invalid

    def locate(obj, path):
        if obj is invalid:
            _catalog_error(path, "число должно быть конечным")
        if isinstance(obj, dict):
            if invalid in obj:
                _catalog_error(f"{path}.{obj[invalid]}", "повтор ключа")
            for key, value in obj.items():
                locate(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                locate(value, f"{path}[{index}]")

    root = json.loads(data, object_pairs_hook=pairs, parse_constant=constant,
                      parse_float=_parse_decimal)
    if seen_invalid:
        locate(root, "$")
    return root


def load_catalog(path):
    with open(path, encoding="utf-8-sig") as stream:
        candidate = _catalog_decode(stream.read())
    validate_catalog(candidate)
    return candidate


def _canon(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        num = Decimal(str(value))
        if not num.is_finite():
            raise ValueError("Неконечное число в JSON")
        if not num:
            return "0"
        whole = format(num, "f")
        return whole.rstrip("0").rstrip(".") if "." in whole else whole
    if isinstance(value, list):
        return "[" + ",".join(_canon(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(_canon(k) + ":" + _canon(value[k])
                              for k in sorted(value)) + "}"
    raise ValueError(f"Неподдерживаемый тип JSON {type(value).__name__}")


def fingerprint(catalog):
    return hashlib.sha256(_canon(catalog).encode("utf-8")).hexdigest()


def validate_catalog(catalog):
    """Schema 1. Error paths use the original (stable) IDs, not array offsets."""
    def mapping(value, path):
        if not isinstance(value, dict):
            _catalog_error(path, "требуется объект")
        return value

    def entries(value, path):
        for key, item in mapping(value, path).items():
            if not isinstance(key, str) or not key.strip():
                _catalog_error(path, "требуется непустой строковый ID")
            p = f"{path}.{key}"
            yield key, mapping(item, p), p

    def array(obj, key, path):
        value = obj.get(key)
        if not isinstance(value, list):
            _catalog_error(f"{path}.{key}", "требуется список")
        return value

    def string(obj, key, path):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            _catalog_error(f"{path}.{key}", "требуется непустая строка")
        return value

    def integer(obj, key, path, minimum=None):
        value = obj.get(key)
        if type(value) is not int or (minimum is not None and value < minimum):
            lower = "" if minimum is None else f" ≥ {minimum}"
            _catalog_error(f"{path}.{key}", "требуется целое число" + lower)
        return value

    def choice(obj, key, path, values):
        value = string(obj, key, path)
        if value not in values:
            _catalog_error(f"{path}.{key}", f"неподдерживаемое значение {value!r}")
        return value

    def flag(obj, key, path):
        if type(obj.get(key)) is not bool:
            _catalog_error(f"{path}.{key}", "требуется true или false")

    def number(value, path):
        finite = (type(value) is int or
                  isinstance(value, float) and math.isfinite(value) or
                  isinstance(value, Decimal) and value.is_finite())
        if not finite:
            _catalog_error(path, "требуется конечное число")
        return value

    def ref(obj, key, path, targets):
        value = string(obj, key, path)
        if value not in targets:
            _catalog_error(f"{path}.{key}", f"неизвестный ID {value!r}")
        return value

    mapping(catalog, "$")
    for key in ("schema_version", "calc_version"):
        if integer(catalog, key, "$") != 1:
            _catalog_error(f"$.{key}", "поддерживается только версия 1")
    integer(catalog, "revision", "$", 1)
    string(catalog, "game_version", "$")
    try:
        dt.date.fromisoformat(string(catalog, "catalog_date", "$"))
    except ValueError:
        _catalog_error("$.catalog_date", "требуется дата ISO")
    for key in ("sources", "stats", "slots", "modules", "weapons", "compatibility"):
        mapping(catalog.get(key), "$." + key)
    sources, stats = catalog["sources"], catalog["stats"]
    modules, slots, weapons = catalog["modules"], catalog["slots"], catalog["weapons"]

    def source(obj, path, required=True):
        if required or "source_id" in obj:
            ref(obj, "source_id", path, sources)
        if "locator" in obj:
            string(obj, "locator", path)

    def fact(obj, path, implied=False):
        source(obj, path)
        state = obj.get("state", "known" if implied else None)
        if state not in ("known", "unknown", "disputed"):
            _catalog_error(path + ".state", "требуется known, unknown или disputed")
        if "value" not in obj:
            _catalog_error(path + ".value", "отсутствует поле")
        if state == "known":
            number(obj["value"], path + ".value")
        elif obj["value"] is not None:
            _catalog_error(path + ".value", "требуется null")
        if state == "disputed":
            string(obj, "reason", path)
            if obj.get("raw_label") is None and obj.get("raw_value") is None:
                _catalog_error(path, "требуется raw_label или raw_value")

    for _, item, path in entries(sources, "$.sources"):
        string(item, "name", path)
        string(item, "date" if "date" in item else "version", path)
        string(item, "url" if "url" in item else "locator", path)

    # Units supported by the published-effect profile and its display scales.
    units = {"pt", "s", "ms", "count", "HP", "rpm", "HP/s", "%", "ratio",
             "deg", "deg/s", "m", "m/s", "1", "shots", "rounds", "x"}
    deps = {}
    for sid, item, path in entries(stats, "$.stats"):
        for key in ("name", "meaning"):
            string(item, key, path)
        choice(item, "unit", path, units)
        choice(item, "direction", path, ("max", "min", "display_only"))
        integer(item, "precision", path, 0)
        kind = choice(item, "kind", path, ("base", "derived"))
        if "scale" in item:
            scale = mapping(item["scale"], path + ".scale")
            lo = number(scale.get("low"), path + ".scale.low")
            hi = number(scale.get("high"), path + ".scale.high")
            if lo >= hi:
                _catalog_error(path + ".scale", "требуется low < high")
        if kind == "base":
            for key in ("formula_id", "dependencies"):
                if key in item:
                    _catalog_error(path + "." + key, "поле предназначено для derived")
        else:
            string(item, "formula_id", path)
            deps[sid] = array(item, "dependencies", path)
            for i, key in enumerate(deps[sid]):
                if not isinstance(key, str) or key not in stats:
                    _catalog_error(f"{path}.dependencies[{i}]", f"неизвестная метрика {key!r}")
    visited, stack = set(), set()

    def visit(sid):
        if sid in stack:
            _catalog_error(f"$.stats.{sid}.dependencies", "цикл зависимостей")
        if sid in visited:
            return
        stack.add(sid)
        for key in deps.get(sid, []):
            visit(key)
        stack.remove(sid)
        visited.add(sid)

    for sid in deps:
        visit(sid)
    formulas = {"fire_rate", "shot_damage", "scenario_shot_damage", "dps", "shots_needed",
                "ttk", "magazine_damage", "scenario_magazine_damage", "magazine_time", "ammo"}
    for sid in deps:
        choice(stats[sid], "formula_id", f"$.stats.{sid}", formulas)
    for _, item, path in entries(slots, "$.slots"):
        string(item, "name", path)
        integer(item, "order", path)
        source(item, path, required=False)

    import_keys = set()
    for mid, item, path in entries(modules, "$.modules"):
        if "import_key" in item:
            ip = path + ".import_key"
            imp = mapping(item["import_key"], ip)
            space = string(imp, "namespace", ip)
            ident = imp.get("external_id")
            if type(ident) not in (int, str) or (isinstance(ident, str) and not ident.strip()):
                _catalog_error(ip + ".external_id", "требуется целый или текстовый внешний ID")
            if (space, ident) in import_keys:
                _catalog_error(ip + ".external_id", "повтор внешнего ID внутри источника")
            import_keys.add((space, ident))
        string(item, "name", path)
        ref(item, "slot_type", path, slots)
        source(item, path)
        for i, effect in enumerate(array(item, "effects", path)):
            p = f"{path}.effects[{i}]"
            mapping(effect, p)
            ref(effect, "stat_id", p, stats)
            op = choice(effect, "op", p, ("mul", "add", "set"))
            if "priority" in effect:
                integer(effect, "priority", p)
                if op != "set":
                    _catalog_error(p + ".priority", "приоритет определён только для set")
            fact(effect, p)
        if "unresolved_effects" in item:
            for i, effect in enumerate(array(item, "unresolved_effects", path)):
                p = f"{path}.unresolved_effects[{i}]"
                mapping(effect, p)
                fact(effect, p)
                choice(effect, "state", p, ("unknown", "disputed"))
                affected = array(effect, "affected_stats", p)
                if not affected or ("*" in affected and affected != ["*"]):
                    _catalog_error(p + ".affected_stats", "требуются ID метрик или [\"*\"]")
                for j, stat in enumerate(affected):
                    if not isinstance(stat, str) or (stat != "*" and stat not in stats):
                        _catalog_error(f"{p}.affected_stats[{j}]", "неизвестная метрика")

    def points(obj, key, path):
        ids = set()
        for i, point in enumerate(array(obj, key, path)):
            p = f"{path}.{key}[{i}]"
            mapping(point, p)
            ident = string(point, "point_id", p)
            if ident in ids:
                _catalog_error(p + ".point_id", "повтор ID точки")
            ids.add(ident)
            string(point, "type", p)
            integer(point, "capacity", p, 1)
            source(point, p, required=False)
            if "virtual" in point:
                flag(point, "virtual", p)

    for wid, item, path in entries(weapons, "$.weapons"):
        string(item, "name", path)
        string(item, "class", path)
        source(item, path)
        flag(item, "points_complete", path)
        points(item, "points", path)
        for _, slot, sp in entries(item.get("slots"), path + ".slots"):
            stype = ref(slot, "type", sp, slots)
            integer(slot, "order", sp)
            flag(slot, "required", sp)
            if "stock_module_id" not in slot:
                _catalog_error(sp + ".stock_module_id", "отсутствует поле")
            if slot["stock_module_id"] is not None:
                mid = ref(slot, "stock_module_id", sp, modules)
                if modules[mid]["slot_type"] != stype:
                    _catalog_error(sp + ".stock_module_id", "тип детали отличается от типа слота")
            source(slot, sp, required=False)
        for sid, value, vp in entries(item.get("values"), path + ".values"):
            if sid not in stats:
                _catalog_error(vp, "неизвестная метрика")
            choice(value, "basis", vp, ("base", "stock"))
            fact(value, vp, implied=True)

    pair_ids = set()
    for _, item, path in entries(catalog["compatibility"], "$.compatibility"):
        wid = ref(item, "weapon_id", path, weapons)
        mid = ref(item, "module_id", path, modules)
        if (wid, mid) in pair_ids:
            _catalog_error(path, f"повтор пары {wid}/{mid}")
        pair_ids.add((wid, mid))
        choice(item, "status", path, ("allowed", "unknown", "forbidden"))
        string(item, "reason", path)
        source(item, path)
        flag(item, "points_complete", path)
        if "stock_root" in item:
            flag(item, "stock_root", path)
        if "mount_unknown" in item:
            string(item, "mount_unknown", path)
        points(item, "provides", path)
        for i, group in enumerate(array(item, "requirements", path)):
            gp = f"{path}.requirements[{i}]"
            if not isinstance(group, list) or not group:
                _catalog_error(gp, "требуется непустой список альтернатив")
            for j, req in enumerate(group):
                rp = f"{gp}[{j}]"
                mapping(req, rp)
                string(req, "type", rp)
                for key in ("provider", "point_id"):
                    if key in req:
                        string(req, key, rp)
                if "provider_slot" in req:
                    ref(req, "provider_slot", rp, weapons[wid]["slots"])
                if "provider_module_id" in req:
                    ref(req, "provider_module_id", rp, modules)
                if "provider_slot_type" in req:
                    ref(req, "provider_slot_type", rp, slots)
                provider = req.get("provider")
                if provider is not None and provider not in ("any", "*", "body"):
                    if provider not in weapons[wid]["slots"] and provider not in modules:
                        _catalog_error(rp + ".provider", "неизвестный поставщик")
                if "virtual" in req:
                    flag(req, "virtual", rp)
                source(req, rp, required=False)
        for i, excl in enumerate(array(item, "excludes", path)):
            ep = f"{path}.excludes[{i}]"
            if isinstance(excl, dict):
                ref(excl, "module_id", ep, modules)
                source(excl, ep, required=False)
            elif not isinstance(excl, str) or excl not in modules:
                _catalog_error(ep, "неизвестная деталь")
    return catalog


class StalePresetError(ValueError):
    pass


class PresetNameError(ValueError):
    def __init__(self, existing):
        super().__init__(f"Имя «{existing['name']}» уже занято")
        self.existing = existing


def _preset_json(value):
    if isinstance(value, (Fraction, Decimal)):
        return str(value)
    raise TypeError(f"Неподдерживаемое значение пресета: {type(value).__name__}")


class Store:
    def __init__(self, path=None):
        if path is None:
            folder = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            base = Path(folder) if folder else Path.home() / ".local" / "share"
            path = base / "BodycamConfigurator" / "presets.sqlite3"
        self.path = str(path)
        self.notice = ""
        self.db = None
        try:
            if self.path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._open(self.path)
        except (OSError, sqlite3.Error) as error:
            if self.db is not None:
                self.db.close()
            self.notice = (f"Хранилище недоступно ({error}). Работа продолжается в памяти. "
                           "Пресеты этой сессии будут потеряны при выходе.")
            self._open(":memory:")

    def _open(self, path):
        self.db = sqlite3.connect(path, timeout=3, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS revisions(
                digest TEXT PRIMARY KEY, game TEXT NOT NULL, rev INTEGER NOT NULL,
                payload BLOB NOT NULL, UNIQUE(game, rev));
            CREATE TABLE IF NOT EXISTS active(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS presets(
                uuid TEXT PRIMARY KEY, name_key TEXT NOT NULL UNIQUE, edit_version INTEGER NOT NULL,
                payload TEXT NOT NULL);
        """)

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def activate(self, catalog):
        validate_catalog(catalog)
        raw = _canon(catalog).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        game, rev = catalog["game_version"], catalog["revision"]
        packed = zlib.compress(raw, 1)
        with self._transaction():
            row = self.db.execute("SELECT digest FROM revisions WHERE game=? AND rev=?",
                                  (game, rev)).fetchone()
            if row and row["digest"] != digest:
                _catalog_error("$.revision", f"содержимое {game}/r{rev} изменилось; увеличьте ревизию")
            if row is None:
                self.db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (digest, game, rev, packed))
            self.db.execute("""INSERT INTO active VALUES(1,?) ON CONFLICT(singleton) DO UPDATE
                               SET digest=excluded.digest WHERE active.digest<>excluded.digest""",
                            (digest,))
        return digest

    def current(self):
        row = self.db.execute("SELECT digest FROM active WHERE singleton=1").fetchone()
        return self.archive(row["digest"]) if row else None

    def archive(self, digest):
        row = self.db.execute("SELECT payload FROM revisions WHERE digest=?", (digest,)).fetchone()
        if row is None:
            return None
        try:
            raw = zlib.decompress(row["payload"])
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("не совпала контрольная сумма")
            return _catalog_decode(raw)
        except (ValueError, UnicodeError, zlib.error) as exc:
            raise ValueError(f"Архив {digest} повреждён: {exc}") from exc

    def load_external(self, path):
        try:
            catalog = load_catalog(path)
            self.activate(catalog)
            return catalog, self.notice
        except (ValueError, OSError, sqlite3.Error) as exc:
            msg = f"Справочник {path}: {exc}."
            try:
                old = self.current()
            except (ValueError, sqlite3.Error) as other:
                return None, f"{msg} {other}. Исправьте файл и перечитайте справочник."
            fallback = ("Открыта последняя рабочая ревизия из архива." if old is not None
                        else "Исправьте файл и перечитайте справочник.")
            return old, " ".join(filter(None, (msg, fallback, self.notice)))

    def list_presets(self):
        rows = self.db.execute("SELECT payload FROM presets ORDER BY name_key, uuid")
        return [json.loads(row["payload"]) for row in rows]

    def get(self, preset_id):
        row = self.db.execute("SELECT payload FROM presets WHERE uuid=?", (preset_id,)).fetchone()
        if row is None:
            raise StalePresetError("Пресет удалён. Создайте копию из открытого редактора.")
        return json.loads(row["payload"])

    def _check_token(self, preset):
        current = self.get(preset["uuid"])
        if current["edit_version"] != preset.get("edit_version"):
            raise StalePresetError("Пресет изменён. Перечитайте запись или сохраните копию.")
        return current

    def _prepare(self, preset):
        result = copy.deepcopy(preset)
        digest = result.get("catalog_hash") or result.get("digest")
        if digest is None:
            row = self.db.execute("SELECT digest FROM active WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("Сначала активируйте справочник")
            digest = row["digest"]
        # For an editor snapshot these fields were captured by the same calculation.
        needs_catalog = not all(key in result for key in
                                ("names", "revision", "calc_version", "game_version", "diagnostics"))
        if needs_catalog:
            catalog = self.archive(digest)
            if catalog is None:
                raise ValueError("Нет архивной ревизии и читаемого снимка")
            wid = result.get("weapon_id")
            weapon = catalog["weapons"].get(wid)
            if weapon is None:
                raise ValueError(f"Потеряна модель {wid}")
            actions = result.get("actions", {})
            snapshot = make_snapshot(Engine(catalog), wid, actions, result.get("locks", {}),
                                     result.get("context", {}), result.get("name", "Сборка"),
                                     result.get("query"), result.get("result"))
            result = {**snapshot, **result}
        result.update(catalog_hash=digest, digest=digest, preset_schema=1)
        result.setdefault("actions", {})
        result.setdefault("locks", result.get("pins", {}))
        result.setdefault("context", {})
        return result

    def save(self, preset, name, replace=False):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Введите имя пресета")
        name = name.strip()
        result = self._prepare(preset)
        with self._transaction():
            original = self._check_token(result) if result.get("uuid") else None
            row = self.db.execute("SELECT payload FROM presets WHERE name_key=?",
                                  (name.casefold(),)).fetchone()
            target = json.loads(row["payload"]) if row else None
            if target and (original is None or target["uuid"] != original["uuid"]):
                if isinstance(replace, dict):
                    if replace.get("uuid") != target["uuid"]:
                        raise StalePresetError("Имя теперь занято другой записью")
                    self._check_token(replace)
                elif replace is not True:
                    raise PresetNameError(target)
                original = target
            elif isinstance(replace, dict) and (not target or target["uuid"] != replace["uuid"]):
                raise StalePresetError("Запись для замены изменилась")
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            result.update(name=name, uuid=original["uuid"] if original else str(uuid.uuid4()),
                          edit_version=original["edit_version"] + 1 if original else 1,
                          created_at=original["created_at"] if original else now, updated_at=now)
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False, default=_preset_json)
            self.db.execute("""INSERT INTO presets VALUES(?,?,?,?) ON CONFLICT(uuid) DO UPDATE
                               SET name_key=excluded.name_key, edit_version=excluded.edit_version,
                                   payload=excluded.payload""",
                            (result["uuid"], name.casefold(), result["edit_version"], raw))
        return json.loads(raw)

    def copy(self, preset, name):
        result = dict(preset)
        for key in ("uuid", "edit_version", "created_at", "updated_at"):
            result.pop(key, None)
        return self.save(result, name)

    def rename(self, preset, name):
        return self.save(preset, name)

    def delete(self, preset):
        with self._transaction():
            self._check_token(preset)
            self.db.execute("DELETE FROM presets WHERE uuid=?", (preset["uuid"],))

    def migrate(self, preset, catalog):
        """Preview a copy. Preserve old labels, lost action IDs, and ranking provenance."""
        result = copy.deepcopy(preset)
        for key in ("uuid", "edit_version", "created_at", "updated_at"):
            result.pop(key, None)
        warnings = []
        lost_slots = {}
        old_effective = preset.get('effective', {})
        old_names = preset.get("names", {})
        wid = preset["weapon_id"]
        weapon = catalog["weapons"].get(wid)
        actions = preset.get("actions", {})
        context = preset.get("context", {})
        pins = preset.get("locks", preset.get("pins", {}))
        if weapon is None:
            lost_slots = dict(old_effective)
            warnings.append(f"Потеряна модель {old_names.get('weapon', wid)} [{wid}]")
            result.update(status="conflict", metrics={}, diagnostics=warnings.copy())
        else:
            s_names = old_names.get("slots", {})
            m_names = old_names.get("modules", {})
            for sid, action in actions.items():
                if sid not in weapon["slots"]:
                    lost_slots[sid] = old_effective.get(sid)
                    warnings.append(f"Потерян слот {s_names.get(sid, sid)} [{sid}]")
                    continue
                mode = action.get("action") if isinstance(action, dict) else action
                mid = action.get("module_id") if isinstance(action, dict) else action
                if mode not in ("stock", "empty", None) and mid not in catalog["modules"]:
                    lost_slots[sid] = mid
                    warnings.append(f"Потеряна деталь {m_names.get(mid, mid)} [{mid}]")
                if mode == "stock" and sid in old_effective:
                    before, after = old_effective[sid], weapon["slots"][sid]["stock_module_id"]
                    if before != after:
                        warnings.append(f"{s_names.get(sid, sid)}: штатная деталь {before} → {after}")
            fresh = make_snapshot(Engine(catalog), wid, actions, pins, context,
                                  preset.get("name", "Копия пресета"))
            result.update(fresh)
            for key in ("slots", "modules"):
                result["names"][key] = {**old_names.get(key, {}), **result["names"].get(key, {})}
                # Preserve a human-readable label even when the new engine only has a lost ID.
                for key_id, label in old_names.get(key, {}).items():
                    if result["names"][key].get(key_id) == key_id:
                        result["names"][key][key_id] = label
            warnings.extend(text for text in result["diagnostics"] if text not in warnings)
            old_metrics, new_metrics = preset.get("metrics", {}), result["metrics"]
            for key in sorted(old_metrics.keys() | new_metrics.keys()):
                old, new = old_metrics.get(key, {}), new_metrics.get(key, {})
                a, b = old.get("value"), new.get("value")
                if (a, old.get("state")) != (b, new.get("state")):
                    name = old.get("name") or new.get("name") or key
                    warnings.append(f"{name}: {a if a is not None else old.get('state', '—')} → "
                                    f"{b if b is not None else new.get('state', '—')}")
            result["diagnostics"] = list(dict.fromkeys(result["diagnostics"] + warnings))
        digest = fingerprint(catalog)
        result.update(catalog_hash=digest, digest=digest, revision=catalog["revision"],
                      game_version=catalog["game_version"], calc_version=catalog["calc_version"],
                      previous_names=old_names, previous_catalog_hash=preset.get("catalog_hash"),
                      migration=warnings, lost_slots=lost_slots, previous_query=preset.get("query"),
                      previous_result=preset.get("result"), query=None, result=None)
        return result


def demo_catalog():
    """Полная синтетическая фикстура А1 из технического задания."""
    return json.loads('''{
  "schema_version": 1, "game_version": "demo", "revision": 1,
  "catalog_date": "2026-11-23", "calc_version": 1,
  "sources": {"s": {"name": "Тест", "version": "1", "locator": "Эталон А1"}},
  "stats": {"index": {
    "name": "Индекс", "meaning": "Условный игровой индекс",
    "unit": "pt", "direction": "max", "precision": 2,
    "kind": "base", "scale": {"low": 0, "high": 20}}},
  "slots": {"acc": {"name": "Аксессуар", "order": 10}},
  "weapons": {"demo": {
    "name": "Демо", "class": "Тест", "source_id": "s",
    "slots": {"a": {"type": "acc", "order": 10,
      "required": false, "stock_module_id": null}},
    "points_complete": true,
    "points": [{"point_id": "p", "type": "socket", "capacity": 1}],
    "values": {"index": {"basis": "base", "value": 10,
      "source_id": "s"}}}},
  "modules": {"m": {
    "name": "Модуль", "slot_type": "acc", "source_id": "s",
    "effects": [{"stat_id": "index", "op": "add", "value": 2,
      "state": "known", "source_id": "s"}]}},
  "compatibility": {"demo_m": {
    "weapon_id": "demo", "module_id": "m", "status": "allowed",
    "reason": "Эталон А1", "source_id": "s", "points_complete": true,
    "requirements": [[{"type": "socket", "provider": "body"}]],
    "provides": [], "excludes": []}}
}''')


def tests_storage():
    """Actual disk transactions in an isolated, short-lived user directory."""
    import tempfile
    from collections import OrderedDict

    def require_rejection(operation, path):
        try:
            operation()
        except (ValueError, sqlite3.Error) as exc:
            assert path in str(exc), (path, str(exc))
        else:
            raise AssertionError(f"Ожидался отказ {path}")

    def candidate_at(catalog, *changes):
        modified = copy.deepcopy(catalog)
        for path, value in changes:
            node = modified
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
        return modified

    results = {}
    with tempfile.TemporaryDirectory(prefix="Каталог пресетов ") as folder:
        path = Path(folder) / "presets.sqlite3"
        external = Path(folder) / "bodycam_catalog.json"
        c1 = demo_catalog()
        store = Store(path)
        h1 = store.activate(c1)
        assert h1 == store.activate(c1)
        modified = candidate_at(c1, (("modules", "m", "effects", 0, "value"), 2.0))
        assert h1 == store.activate(modified)
        assert fingerprint({"x": [0, 2]}) == fingerprint({"x": [-0.0, 2.0]})
        assert fingerprint({"x": [0, 2]}) != fingerprint({"x": [2, 0]})
        assert fingerprint(OrderedDict(reversed(list(c1.items())))) == h1
        assert fingerprint({"s": Decimal("1.100000000000000000000000001")}) != fingerprint({"s": 1.1})
        store.close()

        # Recovery on startup before even the first preset exists.
        external.write_text("{", encoding="utf-8")
        store = Store(path)
        restored, message = store.load_external(external)
        assert restored == c1 and "архива" in message and not store.list_presets()
        invalid_inputs = [
            ((("schema_version",), 2), "$.schema_version"),
            ((("compatibility", "x"), c1["compatibility"]["demo_m"]), "$.compatibility.x"),
            ((("compatibility", "demo_m", "module_id"), "lost"), "$.compatibility.demo_m.module_id"),
            ((("modules", "m", "effects", 0, "value"), None), "$.modules.m.effects[0].value"),
            ((("stats", "index", "precision"), -1), "$.stats.index.precision"),
            ((("stats", "index", "unit"), "invalid-unit"), "$.stats.index.unit"),
            ((("modules", "m", "effects", 0, "op"), "divide"), "$.modules.m.effects[0].op"),
            ((("modules", "m", "effects", 0, "value"), 7), "$.revision")
        ]
        for change, error in invalid_inputs:
            require_rejection(lambda: store.activate(candidate_at(c1, change)), error)
            assert store.current() == c1
        cyc = copy.deepcopy(c1)
        cyc["stats"]["cycle"] = dict(cyc["stats"]["index"], kind="derived",
                                     formula_id="fire_rate", dependencies=["cycle"])
        require_rejection(lambda: store.activate(cyc), "$.stats.cycle.dependencies")
        external.write_text('{"weapons":{"demo":{"points":[{"type":1,"type":2}]}}}',
                            encoding="utf-8")
        require_rejection(lambda: load_catalog(external), "$.weapons.demo.points[0].type")
        assert store.current() == c1
        results["Д3"] = ("Дубли вложенного ключа и пары, потерянный ID, неверные схема, единица, "
                         "операция, значение, точность, цикл и коллизия ревизии отклонены по адресу. "
                         "Активная ревизия 1 восстановлена.")

        pins = {"a": {"action": "explicit", "module_id": "m"}}
        actions = copy.deepcopy(pins)
        terms = {"mode": "weighted", "criteria": [{"stat": "index", "weight": 2,
                 "low": 0, "high": 20}], "pins": pins}
        state = make_snapshot(Engine(c1), "demo", actions, pins, {}, "Эталон",
                              terms, {"complete": False, "score": "60"})
        original = store.save(state, "  Проверка  ")
        assert original["name"] == "Проверка" and original["edit_version"] == 1
        assert original["metrics"]["index"]["value"] == "12"
        store.close()
        store = Store(path)
        reopened = store.get(original["uuid"])
        assert reopened == original and reopened["actions"] == actions and reopened["pins"] == pins
        assert reopened["query"] == terms and reopened["result"]["complete"] is False
        reopened = store.rename(reopened, "Первый")
        new = store.copy(reopened, "Копия")
        assert new["uuid"] != reopened["uuid"]
        require_rejection(lambda: store.save(state, " первый "), "занято")
        overwritten = store.save(state, "пЕрвый", replace=reopened)
        assert overwritten["uuid"] == reopened["uuid"] and overwritten["name"] == "пЕрвый"
        second = Store(path)
        stale = second.get(overwritten["uuid"])
        renamed = store.rename(overwritten, "Новый")
        require_rejection(lambda: second.rename(stale, "Давно"), "Перечитайте")
        require_rejection(lambda: second.delete(stale), "Перечитайте")
        copied = second.copy(stale, "Резервная")
        assert copied["uuid"] != renamed["uuid"]
        # Force a failure after INSERT archive but before switching active revision.
        c2 = candidate_at(c1, (("revision",), 2), (("modules", "m", "effects", 0, "value"), -1))
        store.db.execute("""CREATE TEMP TRIGGER interrupt_switch BEFORE UPDATE ON active
                            BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
        require_rejection(lambda: store.activate(c2), "test rollback")
        assert store.current() == c1 and store.archive(fingerprint(c2)) is None
        store.db.execute("DROP TRIGGER interrupt_switch")
        store.activate(c2)
        assert store.archive(h1) == c1 and store.current() == c2
        migrated = store.migrate(renamed, c2)
        assert migrated["metrics"]["index"]["value"] == "9" and migrated["migration"]
        assert renamed["metrics"]["index"]["value"] == "12" and "uuid" not in migrated
        saved_copy = store.save(migrated, "Новая ревизия")
        assert saved_copy["uuid"] != renamed["uuid"] and saved_copy["previous_query"] == terms
        lost = candidate_at(c2, (("revision",), 3), (("modules",), {}), (("compatibility",), {}))
        store.activate(lost)
        missing_module = store.migrate(renamed, lost)
        assert missing_module["status"] == "unknown" and missing_module["actions"] == actions
        assert missing_module["lost_slots"] == {"a": "m"}
        assert any("Модуль" in d for d in missing_module["migration"])
        lost_model = candidate_at(lost, (("revision",), 4), (("weapons",), {}))
        store.activate(lost_model)
        missing_weapon = store.migrate(renamed, lost_model)
        assert any("Демо" in d for d in missing_weapon["migration"])
        assert missing_weapon["status"] == "conflict" and not missing_weapon["metrics"]
        assert store.save(missing_weapon, "Потерянная модель")["names"]["modules"]["m"] == "Модуль"
        # A preset update aborted inside a transaction leaves its prior snapshot intact.
        store.db.execute("""CREATE TEMP TRIGGER interrupt_save BEFORE UPDATE ON presets
                            BEGIN SELECT RAISE(ABORT,'test preset rollback'); END""")
        require_rejection(lambda: store.rename(renamed, "Сбой"), "test preset rollback")
        assert store.get(renamed["uuid"]) == renamed
        store.db.execute("DROP TRIGGER interrupt_save")
        second.delete(new)
        require_rejection(lambda: store.get(new["uuid"]), "удалён")
        # Simulate loss of the original archive without losing the readable preset.
        second.db.execute("DELETE FROM revisions WHERE digest=?", (h1,))
        assert store.archive(h1) is None
        assert store.get(renamed["uuid"])["names"]["weapon"] == "Демо"
        second.close()
        store.close()
        memory = Store(path / "cannot-create" / "presets.sqlite3")
        assert memory.notice and memory.activate(c1) == h1 and memory.current() == c1
        memory.close()
        results["П2"] = ("Два подключения: устаревшие правка и удаление отклонены. Запись открыта "
                         "после перезапуска; перенос 12→9, утраты модели и детали читаемы; "
                         "имя, копия, замена и удаление проверены. Оба прерывания откатились, "
                         "недоступный каталог переключён в память.")
    return results


# Workbook adapter for the Bodycam 0.9.1 game snapshot.
import csv
import json
import math
import re
import unicodedata
from pathlib import Path


_BOOK_SOURCE = "workbook_091"
_BOOK_CLASSES = ("ПП", "Автоматы", "Пистолеты", "Снайперские винтовки",
                 "Дробовики", "Арбалеты", "Револьверы")
_BOOK_NAMES = ("SKR-H|M4|M15 IRS|M16-1A|KA-74|Drako|KA-74M|KA-US|Veaper|UZ-1|"
               "USG-45|SG9-X|SG5-K|SG5-KF|Skorpion|RaidX|R12|Vaiga|BK-101|BK-102S|"
               "LAR|VSD|Rivington 700|TAC Rivington|Tenpoint Stealth|Explosive Crossbow|"
               "Deagle|CR-75|M1914|Mlock19|TK-9|FX-45|Revolver").split("|")
_BOOK_SLOTS = ("Body|Barrel|Threaded Barrel|Forearm|Pump|Muzzle|Underbarrel|Bipod|"
               "Upperbarrel|Canted Upperbarrel Mount|Side Mount|Siderail|Optic Mount|Optic|"
               "Canted Optic Mount|Canted Optic|Reticle|Reticle Canted|Reticle Magnified|"
               "Grip|Stock|Magazine|Ammo|Slide|Trigger|Hammer|Cylinder|Bolt Handle|Led|"
               "FPV Camera Support|Other").split("|")
_BOOK_FIELDS = ("ID|Слот|Модуль для выбора|Подкатегория|Совместимое оружие|Условие / сведения|"
                "Эффекты из источника|Источник|Требует|Открывает|Крепления неизвестны").split("|")
# Source header, stable metric ID, display name, unit, preferred direction.
_BOOK_STATS = [
    ("ADSSpeed", "ads_speed", "Скорость входа в ADS", "pt", "max"),
    ("SwitchSpeed", "switch_speed", "Скорость доставания", "pt", "max"),
    ("ReloadTime", "reload_time", "Время перезарядки", "s", "min"),
    ("VerticalRecoil", "vertical_recoil", "Вертикальная отдача модели", "pt", "min"),
    ("CameraRecoilStepVertical", "camera_recoil_vertical", "Вертикальная отдача камеры", "pt", "min"),
    ("HorizontalRecoil", "horizontal_recoil", "Горизонтальная отдача модели", "pt", "min"),
    ("CameraRecoilStepHorizontal", "camera_recoil_horizontal", "Горизонтальная отдача камеры", "pt", "min"),
    ("KickHip", "kick_hip", "Подброс от бедра", "pt", "min"),
    ("KickUp", "kick_up", "Подброс в ADS", "pt", "min"),
    ("RecoilRecoverySpeed", "recovery_speed", "Скорость восстановления", "pt", "max"),
    ("SpreadHipX", "spread_hip_x", "Разброс от бедра X", "pt", "min"),
    ("SpreadHipY", "spread_hip_y", "Разброс от бедра Y", "pt", "min"),
    ("SpreadAimX", "spread_aim_x", "Разброс в ADS X", "pt", "min"),
    ("SpreadAimY", "spread_aim_y", "Разброс в ADS Y", "pt", "min"),
    ("MagnifiedKickHip", "magnified_kick_hip", "Подброс с оптикой от бедра", "pt", "min"),
    ("Speed", "movement_speed", "Скорость движения", "pt", "max"),
    ("Instability", "instability", "Нестабильность", "pt", "min"),
    ("FireRate", "shot_interval", "Интервал между выстрелами", "s", "min"),
    ("Damage", "damage", "Урон одного снаряда", "HP", "max"),
    ("AmmoCapacity", "capacity", "Ёмкость", "count", "max"),
    ("MagAmount", "reserves", "Запасные полные магазины", "count", "max"),
    ("PelletCount", "projectiles", "Снарядов за выстрел", "count", "max"),
]
_BOOK_DERIVED = [
    ("fire_rate", "Темп стрельбы", "rpm", "max", ["shot_interval"]),
    ("shot_damage", "Полный урон выстрела", "HP", "max", ["damage", "projectiles"]),
    ("scenario_shot_damage", "Урон выстрела по сценарию", "HP", "max", ["damage", "projectiles"]),
    ("dps", "Полный урон в секунду", "HP/s", "max", ["shot_damage", "shot_interval"]),
    ("shots_needed", "Выстрелы до достижения цели", "count", "min", ["scenario_shot_damage"]),
    ("magazine_damage", "Полный урон магазина", "HP", "max", ["capacity", "shot_damage"]),
    ("scenario_magazine_damage", "Урон магазина по сценарию", "HP", "max", ["capacity", "scenario_shot_damage"]),
    ("magazine_time", "От первого до последнего выстрела магазина", "s", "min", ["capacity", "shot_interval"]),
    ("ammo", "Общий боезапас", "count", "max", ["capacity", "reserves"]),
    ("ttk", "Время до достижения условной цели", "s", "min", ["shots_needed", "ammo", "shot_interval"]),
]
_BOOK_OPS = (("×", "mul"), ("+", "add"), ("=", "set"))


def _book_norm(value):
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return " ".join(text.split())


def _book_id(name):
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def _book_locator(cell):
    return f"{cell.parent.title}!{cell.coordinate}"


def _book_number(cell, optional=False):
    value = cell.value
    if value is None and optional:
        return None
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"{_book_locator(cell)}: требуется конечное число, получено {value!r}")
    return value


def _book_headers(sheet, width):
    result = {}
    for col in range(1, width + 1):
        cell = sheet.cell(4, col)
        if not isinstance(cell.value, str) or not cell.value.strip():
            raise ValueError(f"{_book_locator(cell)}: отсутствует заголовок")
        header = _book_norm(cell.value)
        if header in result:
            raise ValueError(f"{_book_locator(cell)}: повтор заголовка {cell.value!r}")
        result[header] = col
    return result


def _book_require(sheet, headers, expected):
    for name in expected:
        if _book_norm(name) not in headers:
            raise ValueError(f"{sheet.title}!4: отсутствует заголовок {name!r}")


def _book_tokens(cell):
    if cell.value in (None, "?"):
        return []
    if not isinstance(cell.value, str) or not re.fullmatch(r"\|(?:[a-z_]+\|)+", cell.value):
        raise ValueError(f"{_book_locator(cell)}: неизвестная запись точек {cell.value!r}")
    tokens = cell.value.strip("|").split("|")
    if len(tokens) != len(set(tokens)):
        raise ValueError(f"{_book_locator(cell)}: повтор типа точки без ёмкости")
    return tokens


def _book_stats():
    result = {}
    base = _BOOK_STATS + [
        (None, "ads_time", "Абсолютное время ADS", "s", "min"),
        (None, "reload_speed", "Скорость перезарядки", "pt", "max"),
    ]
    for header, key, name, unit, direction in base:
        result[key] = dict(name=name, meaning=f"{name} в игровой модели Bodycam",
                           unit=unit, direction=direction, precision=3 if unit == "s" else 2,
                           kind="base", positive_base=unit == "pt")
    for key, name, unit, direction, dependencies in _BOOK_DERIVED:
        result[key] = dict(name=name, meaning=name + " для выбранного игрового сценария",
                           unit=unit, direction=direction, precision=3 if unit == "s" else 2,
                           kind="derived", formula_id=key, dependencies=dependencies)
    return result


def _book_mapping(active):
    names = {_book_id(n): [n] for n in _BOOK_NAMES}
    for key, weapon in (active or {}).get("weapons", {}).items():
        if key in names:
            names[key] = list(dict.fromkeys(names[key] + [weapon["name"]] + weapon.get("aliases", [])))
    ids = {}
    for key, labels in names.items():
        for label in labels:
            name = _book_norm(label)
            if name in ids and ids[name] != key:
                raise ValueError(f"/weapons/{key}/aliases: неоднозначное соответствие {label!r}")
            ids[name] = key
    return names, ids


def _book_class_data(wb, ids):
    classes = {}
    for name in _BOOK_CLASSES:
        sh = wb[name]
        rules = [v for v in sh.data_validations.dataValidation if "B4" in v.cells]
        if len(rules) != 1 or not str(rules[0].formula1).startswith('"'):
            raise ValueError(f"{name}!B4: не найдена проверка списка моделей")
        for label in rules[0].formula1.strip('"').split(","):
            wid = ids.get(_book_norm(label))
            if wid is None or wid in classes:
                raise ValueError(f"{name}!B4: модель {label!r} неизвестна или повторяется")
            classes[wid] = name
    return classes


class _BookReport:
    """Each source cell in the prescribed rectangles is described exactly once."""
    def __init__(self):
        self.cells = {}

    def put(self, cell, dest, state, reason, metrics=()):
        key = _book_locator(cell)
        if key in self.cells:
            raise ValueError(f"{key}: ячейка обработана дважды")
        self.cells[key] = (cell.value, dest, state, reason, list(metrics))

    @staticmethod
    def read(tree, parts):
        current = tree
        for key in parts:
            try:
                current = current[key]
            except (KeyError, TypeError, IndexError):
                return "<поле отсутствует>"
        return current

    def write(self, path, cat, active):
        path = Path(path)
        encode = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("locator", "raw", "destination", "state", "reason", "affected_stats",
                             "candidate", "active", "different"))
            for addr, (raw, dest, state, why, stats) in self.cells.items():
                target = "/" + "/".join(str(k) for k in dest) if dest else ""
                now = self.read(cat, dest) if dest else None
                old = self.read(active, dest) if active and dest else None
                different = ("yes" if now != old else "no") if active and dest else ""
                writer.writerow((addr, encode(raw), target, state, why, encode(stats),
                                 encode(now) if dest else "", encode(old) if active and dest else "", different))


def _book_operations(headers, stock=False):
    result = []
    for header, sid, *_ in _BOOK_STATS[:-1]:
        for symbol, op in _BOOK_OPS:
            name = f"{header} {symbol}" + (" штат." if stock else "")
            result.append((headers[_book_norm(name)], sid, op))
    return result


# Ambiguous upstream labels can correspond to separate workbook channels.
_BOOK_LABELS = {
    "ADS Speed": ("ads_speed",), "Equip Speed": ("switch_speed",),
    "Reload Speed": ("reload_time",), "Fire Rate": ("shot_interval",),
    "Horizontal Recoil": ("horizontal_recoil", "camera_recoil_horizontal"),
    "Vertical Recoil": ("vertical_recoil", "camera_recoil_vertical"),
    "Hip Fire Spread": ("spread_hip_x", "spread_hip_y"),
    "ADS Spread": ("spread_aim_x", "spread_aim_y"),
    "Weapon Kick": ("kick_hip",), "KickUp": ("kick_up",),
    "Recoil Recovery": ("recovery_speed",), "Movement Speed": ("movement_speed",),
    "Magnified Kick Hip": ("magnified_kick_hip",), "Instability": ("instability",),
    "Mag Capacity": ("capacity",), "Mag Amount": ("reserves",), "Bullet Damage": ("damage",),
}


def _book_check_labels(module, row, columns, label_cell):
    """Reconcile the published text against individual numeric channels and operations."""
    text = label_cell.value
    if text == "Нет опубликованных изменений" or "не подтверждены" in text.casefold():
        return
    used = set()
    for token in text.split(" / "):
        match = re.fullmatch(r"(.+?)\s*([+−=\-])\s*(\d+(?:\.\d+)?)(%?)", token.strip())
        stats = _BOOK_LABELS.get(match[1], ()) if match else ()
        involved = []
        if stats:
            amount = float(match[3])
            if match[2] in ("-", "−"):
                amount = -amount
            if match[4]:
                op, target = "mul", 1 + amount / 100
            elif match[2] == "=":
                op, target = "set", amount
            else:
                op, target = "add", amount
            involved = [(col, sid) for col, sid, opname in columns if sid in stats and opname == op]
            candidates = [(col, sid) for col, sid in involved if col not in used]
            found = next(((col, sid) for col, sid in candidates if row[col - 1].value is not None
                          and math.isclose(row[col - 1].value, target, rel_tol=1e-12, abs_tol=1e-12)), None)
            if found:
                col, sid = found
                used.add(col)
                if (op, target) in (("add", 0), ("mul", 1)):
                    module["effects"].append(dict(stat_id=sid, state="known", source_id=_BOOK_SOURCE,
                                                  op=op, value=row[col - 1].value,
                                                  locator=f"{_book_locator(label_cell)},{row[col-1].coordinate}"))
                continue
        pointers = [_book_locator(label_cell)] + [row[col-1].coordinate for col, _ in involved]
        module["unresolved_effects"].append(dict(
            state="disputed", value=None, raw_label=token, source_id=_BOOK_SOURCE,
            locator=",".join(pointers), affected_stats=list(stats) or ["*"],
            reason="Подпись эффекта не имеет однозначного подтверждения в числовых колонках строки"))


def _book_effects(row, columns, module, base, label_cell, report):
    effects = module["effects"]
    unresolved = module["unresolved_effects"]
    label = label_cell.value
    if "не подтверждены" in label.casefold():
        unresolved.append(dict(state="unknown", value=None, raw_label=label, affected_stats=["*"],
                               source_id=_BOOK_SOURCE, locator=_book_locator(label_cell),
                               reason="Книга указывает на отсутствие подтверждения для патча 0.9.1"))
    for col, sid, op in columns:
        cell = row[col - 1]
        value = _book_number(cell, optional=True)
        if value is None or (op == "mul" and value == 1) or (op == "add" and value == 0):
            why = "Нет операции set" if value is None else "Нейтральный шаблон числовой колонки"
            report.put(cell, (), "excluded", why, [sid])
            continue
        affected = []
        if sid == "shot_interval" and "fire rate" in label.casefold():
            affected = ["fire_rate", "shot_interval"]
            why = "Множитель частоты и изменение интервала требуют подтверждения направления"
        elif sid == "reload_time" and "reload speed" in label.casefold():
            affected = ["reload_time", "reload_speed"]
            why = "Подпись задаёт скорость, заголовок числовой колонки — длительность"
        if affected:
            addresses = f"{_book_locator(label_cell)},{cell.coordinate}"
            idx = len(unresolved)
            unresolved.append(dict(state="disputed", value=None, raw_label=label, raw_value=value,
                                   affected_stats=affected, reason=why, source_id=_BOOK_SOURCE,
                                   locator=addresses))
            report.put(cell, (*base, "unresolved_effects", idx, "raw_value"), "disputed", why, affected)
        else:
            idx = len(effects)
            effects.append(dict(stat_id=sid, op=op, state="known", value=value,
                                source_id=_BOOK_SOURCE, locator=_book_locator(cell)))
            report.put(cell, (*base, "effects", idx, "value"), "known", op, [sid])


def _book_pairs(sheet, row, columns, module, module_id, report, applicable):
    get = lambda name: row[columns[_book_norm(name)] - 1]
    req, prov, flag = (get(n) for n in _BOOK_FIELDS[8:11])
    required_types, provided_types = _book_tokens(req), _book_tokens(prov)
    if flag.value not in (0, 1):
        raise ValueError(f"{_book_locator(flag)}: ожидается 0 или 1")
    slot = module["slot_type"]
    requirements = []
    excel_row = 8 + next(i for i, name in enumerate(_BOOK_SLOTS) if _book_id(name) == slot)
    for t in required_types:
        rule = f"{_book_locator(req)}; ПП!AH{excel_row}"
        options = [dict(type=t, provider="body", locator=rule)]
        for typ in ("barrel", "threaded_barrel", "forearm", "pump", "body"):
            if typ != slot:
                options.append(dict(type=t, provider_slot_type=typ, locator=rule))
        requirements.append(options)
    provides = [dict(type=t, point_id=t, capacity=1, locator=_book_locator(prov)) for t in provided_types]
    # The optics rules test presence, independently for each reticle (virtual conditions).
    reticle_rules = {"reticle": ("optic", 24), "reticle_canted": ("canted_optic", 25),
                     "reticle_magnified": ("optic", 26)}
    if slot in reticle_rules:
        owner, excel_row = reticle_rules[slot]
        requirements.append([dict(type=slot + "_enabled", provider_slot=owner,
                                  virtual=True, locator=f"ПП!AH{excel_row}")])
    for target, (owner, excel_row) in reticle_rules.items():
        if owner == slot:
            provides.append(dict(type=target + "_enabled", point_id=target, capacity=1,
                                 virtual=True, locator=f"ПП!AH{excel_row}"))
    pairs = {}
    labels = {"✓": ("allowed", "указано в источнике"), "?": ("unknown", "требуется проверка"),
              "—": ("unknown", "нет указания")}
    for wid, col in sheet.items():
        cell = row[col - 1]
        if cell.value not in labels:
            raise ValueError(f"{_book_locator(cell)}: неизвестная отметка допуска {cell.value!r}")
        status, reason = labels[cell.value]
        key = f"{wid}__{module_id}"
        pair = dict(weapon_id=wid, module_id=module_id, status=status, reason=reason,
                    source_id=_BOOK_SOURCE, locator=_book_locator(cell),
                    requirements=requirements, provides=provides, excludes=[],
                    points_complete=flag.value == 0, points_complete_locator=_book_locator(flag))
        if slot == "underbarrel" and wid in ("sg5_k", "sg5_kf") and required_types:
            pair["requirements"] = [[dict(type=t, provider_slot="forearm",
                                          locator=f"{_book_locator(req)}; ПП!AH14")]
                                    for t in required_types]
        if module["condition"].startswith("Mounts on other parts") and not required_types:
            pair["mount_unknown"] = f"{_book_locator(get('Условие / сведения'))}: не указан конкретный поставщик"
        if cell.value != "—":
            applicable[wid].add(slot)
        pairs[key] = pair
        report.put(cell, ("compatibility", key, "status"), status, reason)
    return pairs


def _book_modules(cat, wb, headers, model_cols, report, applicable, active):
    m = wb["Справочник"]
    operations = _book_operations(headers)
    module_ids = {}
    prior_ids = {}
    for key, mod in (active or {}).get("modules", {}).items():
        identity = mod.get("import_key")
        if isinstance(identity, dict) and identity.get("namespace") == "bodycam-stathub-modules":
            number = identity.get("external_id")
            if number in prior_ids:
                raise ValueError(f"/modules/{key}/import_key: неоднозначный внешний ID {number}")
            prior_ids[number] = key
    zero_rows = 0
    slot_names = {_book_norm(n): _book_id(n) for n in _BOOK_SLOTS}
    for row in m.iter_rows(min_row=5, max_row=526, max_col=107):
        get = lambda name: row[headers[_book_norm(name)] - 1]
        external = get("ID")
        number = external.value
        if type(number) is not int or number < 0 or number in module_ids:
            raise ValueError(f"{_book_locator(external)}: ошибочный или повторный ID {number!r}")
        if number == 0:
            zero_rows += 1
            for cell in row:
                report.put(cell, (), "excluded", "Служебная строка ID 0")
            continue
        key = prior_ids.get(number, f"m{number}")
        if key in cat["modules"]:
            raise ValueError(f"{_book_locator(external)}: коллизия стабильного ID {key}")
        module_ids[number] = key
        base = ("modules", key)
        slot_cell = get("Слот")
        slot_id = slot_names.get(_book_norm(slot_cell.value))
        if not slot_id:
            raise ValueError(f"{_book_locator(slot_cell)}: неизвестный слот")
        module = dict(slot_type=slot_id, source_id=_BOOK_SOURCE, locator=_book_locator(external),
                      import_key=dict(namespace="bodycam-stathub-modules", external_id=number),
                      effects=[], unresolved_effects=[])
        cat["modules"][key] = module
        report.put(external, (*base, "import_key", "external_id"), "known", "ID в пространстве импортера")
        report.put(slot_cell, (*base, "slot_type"), "mapped", "Тип слота")
        for field, attr in zip(_BOOK_FIELDS[2:], ("name", "subcategory", "weapon_list", "condition",
                                                 "raw_label", "upstream_source", "raw_requires",
                                                 "raw_provides", "raw_unknown_points")):
            cell = get(field)
            if attr in ("name", "raw_label", "slot_type") and not isinstance(cell.value, str):
                raise ValueError(f"{_book_locator(cell)}: отсутствует текст {field}")
            module[attr] = cell.value
            report.put(cell, (*base, attr), "raw", "Исходное поле справочника")
        _book_effects(row, operations, module, base, get("Эффекты из источника"), report)
        label_cell = get("Эффекты из источника")
        _book_check_labels(module, row, operations, label_cell)
        if module["unresolved_effects"]:
            addr = _book_locator(label_cell)
            raw, dest, _, _, _ = report.cells[addr]
            blocked = sorted({stat for fact in module["unresolved_effects"] for stat in fact["affected_stats"]})
            reasons = dict.fromkeys(fact["reason"] for fact in module["unresolved_effects"])
            state = "disputed" if any(f["state"] == "disputed" for f in module["unresolved_effects"]) else "unknown"
            report.cells[addr] = (raw, dest, state, " / ".join(reasons), blocked)
        pairs = _book_pairs(model_cols, row, headers, module, key, report, applicable)
        cat["compatibility"].update(pairs)
    if zero_rows != 1:
        raise ValueError("Справочник!A5:A526: ожидается одна служебная строка ID 0")
    return module_ids


def _book_weapons(cat, wb, headers, aliases, ids, classes, applicable, modules, report):
    d = wb["Данные"]
    stock_columns = _book_operations(headers, stock=True)
    stat_columns = [(headers[_book_norm(h)], sid) for h, sid, *_ in _BOOK_STATS]
    slot_info = []
    for index, name in enumerate(_BOOK_SLOTS):
        source_row = 8 + index
        info_sheet = wb["ПП"]
        formula = str(info_sheet.cell(source_row, 33).value)
        match = re.search(r'AND\(B\d+="—",([01]),AA\d+>0\)', formula)
        if not match:
            raise ValueError(f"ПП!AG{source_row}: изменилась формула обязательности")
        sid = _book_id(name)
        cat["slots"][sid] = dict(name=info_sheet.cell(source_row, 1).value, order=index + 1,
                                 source_id=_BOOK_SOURCE, locator=f"ПП!A{source_row}")
        slot_info.append((sid, headers[_book_norm(name)], match[1] == "1", source_row))
    for row in d.iter_rows(min_row=5, max_row=37, max_col=119):
        get = lambda name: row[headers[_book_norm(name)] - 1]
        cell = get("Оружие")
        wid = ids.get(_book_norm(cell.value))
        if not wid or wid in cat["weapons"] or wid not in classes:
            raise ValueError(f"{_book_locator(cell)}: неизвестная или повторная модель {cell.value!r}; проверьте aliases")
        base = ("weapons", wid)
        body = get("Крепления корпуса")
        tokens = _book_tokens(body)
        points = [dict(type=t, point_id=t, capacity=1, locator=_book_locator(body)) for t in tokens]
        weapon = dict(name=cell.value, aliases=aliases[wid], source_id=_BOOK_SOURCE,
                      locator=_book_locator(cell), points=points,
                      points_complete=body.value not in ("?", None), slots={}, values={},
                      stock_operations={}, upstream_locator=get("Источник").value)
        weapon["class"] = classes[wid]
        weapon["class_locator"] = classes[wid] + "!B4 (список проверки данных)"
        cat["weapons"][wid] = weapon
        report.put(cell, (*base, "name"), "mapped", "Модель по явному списку имён и псевдонимов")
        report.put(body, (*base, "points"), "mapped" if weapon["points_complete"] else "unknown",
                   "Типы точек корпуса; знак ? означает неполный перечень")
        report.put(get("Источник"), (*base, "upstream_locator"), "known", "Исходный адрес из книги")
        applicable[wid].update(tokens)
        if "reticle_canted" in applicable[wid]:
            applicable[wid].add("canted_optic")
        for sid, col, must, excel_row in slot_info:
            cell = row[col - 1]
            number = cell.value
            if type(number) is not int or number < 0 or (number and number not in modules):
                raise ValueError(f"{_book_locator(cell)}: отсутствующий внешний ID {number!r}")
            if not number and sid not in applicable[wid]:
                report.put(cell, (), "excluded", "Нет штатной детали, точки корпуса и отмеченных для слота пар")
                continue
            stock = modules.get(number)
            if stock and cat["modules"][stock]["slot_type"] != sid:
                raise ValueError(f"{_book_locator(cell)}: деталь {stock} не принадлежит типу {sid}")
            weapon["slots"][sid] = dict(type=sid, order=cat["slots"][sid]["order"],
                                        required=must and bool(stock), stock_module_id=stock,
                                        source_id=_BOOK_SOURCE, locator=_book_locator(cell),
                                        required_locator=f"{classes[wid]}!AG{excel_row}")
            if stock:
                pair = cat["compatibility"][f"{wid}__{stock}"]
                pair["stock_root"] = sid not in ("reticle", "reticle_canted", "reticle_magnified")
                pair["stock_locator"] = _book_locator(cell)
            report.put(cell, (*base, "slots", sid, "stock_module_id"), "mapped", "ID 0 = штатно пусто")
        for col, sid in stat_columns:
            cell = row[col - 1]
            val = _book_number(cell, optional=True)
            state = "unknown" if val is None else "known"
            fact = dict(state=state, value=val, basis="stock", source_id=_BOOK_SOURCE, locator=_book_locator(cell))
            if val is None:
                fact["reason"] = "Числовое штатное значение не опубликовано"
            weapon["values"][sid] = fact
            report.put(cell, (*base, "values", sid, "value"), state,
                       "Штатное наблюдение" if val is not None else fact["reason"], [sid])
        for col, sid, op in stock_columns:
            cell = row[col - 1]
            val = _book_number(cell, optional=True)
            ops = weapon["stock_operations"].setdefault(sid, {})
            ops[op] = val
            report.put(cell, (*base, "stock_operations", sid, op), "raw",
                       "Контрольный агрегат штатных операций; не применять второй раз", [sid])


def import_xlsx(path, active=None, report_path=None):
    """Build a separate game catalog in memory and optionally audit the primary cell ranges."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("Для импорта XLSX установите openpyxl: python -m pip install openpyxl") from exc
    wb = load_workbook(path, read_only=False, data_only=False)
    try:
        required = {"Данные", "Справочник", *_BOOK_CLASSES}
        if missing := required.difference(wb.sheetnames):
            raise ValueError("Отсутствуют листы: " + ", ".join(sorted(missing)))
        d, m = wb["Данные"], wb["Справочник"]
        if not re.search(r"\b0\.9\.1\b", str(m["A2"].value)):
            raise ValueError("Справочник!A2: импортер поддерживает снимок 0.9.1")
        dh, mh = _book_headers(d, 119), _book_headers(m, 107)
        triplets = [f"{h} {symbol}" for h, *_ in _BOOK_STATS[:-1] for symbol, _ in _BOOK_OPS]
        _book_require(d, dh, ["Оружие", "Крепления корпуса", "Источник", *_BOOK_SLOTS,
                              *(s[0] for s in _BOOK_STATS), *(t + " штат." for t in triplets)])
        _book_require(m, mh, _BOOK_FIELDS + triplets)
        aliases, ids = _book_mapping(active)
        model_cols = {}
        for name, col in mh.items():
            if name in ids:
                key = ids[name]
                if key in model_cols:
                    raise ValueError(f"Справочник!{m.cell(4,col).coordinate}: неоднозначная модель {name}")
                model_cols[key] = col
        if missing_ids := set(aliases).difference(model_cols):
            raise ValueError("Справочник!4: не найдены заголовки моделей " + ", ".join(sorted(missing_ids)))
        classes = _book_class_data(wb, ids)
        catalog = dict(schema_version=1, game_version="0.9.1", revision=1, calc_version=1,
                       catalog_date="2026-11-17", stats=_book_stats(), slots={}, weapons={},
                       modules={}, compatibility={},
                       sources={_BOOK_SOURCE: dict(name="Bodycam_Loadout_0.9.1.xlsx",
                                                   version="0.9.1", locator="Данные!A4:DO37; Справочник!A4:DC526")})
        report = _BookReport()
        for sheet, width in ((d, 119), (m, 107)):
            for cell in next(sheet.iter_rows(min_row=4, max_row=4, max_col=width)):
                report.put(cell, (), "header", "Заголовок канонической карты столбцов")
        applicable = {wid: set() for wid in model_cols}
        modules = _book_modules(catalog, wb, mh, model_cols, report, applicable, active)
        _book_weapons(catalog, wb, dh, aliases, ids, classes, applicable, modules, report)
        if len(report.cells) != 119 * 34 + 107 * 523:
            raise ValueError("Не все ячейки исходных диапазонов получили запись сверки")
        if report_path is not None:
            report.write(report_path, catalog, active)
        return catalog
    finally:
        wb.close()


# Проверка состава, назначение точек и точный расчёт игровых показателей.
from collections import defaultdict
from fractions import Fraction
from functools import lru_cache
import math


@lru_cache(maxsize=8192)
def game_number(value):
    """Десятичная запись из JSON имеет то же точное значение в расчёте."""
    if isinstance(value, bool):
        raise ValueError("Ожидалось число")
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


def metric_known(value):
    return {"state": "known", "value": value if isinstance(value, Fraction) else game_number(value)}


def metric_missing(reason, state="unknown", **fields):
    return {"state": state, "value": None, "reason": reason, **fields}


def selected_module(slot_def, action):
    if isinstance(action, dict):
        kind = action.get("action", action.get("kind"))
        if kind == "explicit":
            return action.get("module_id")
        action = kind
    if action == "stock":
        return slot_def.get("stock_module_id")
    if action is None or action == "empty":
        return None
    return action


def _source_note(record):
    return record.get("locator") or record.get("source_id") or ""


def _provider_matches(alt, owner, module, owner_type):
    if "provider_slot" in alt and alt["provider_slot"] != owner:
        return False
    if "provider_module_id" in alt and alt["provider_module_id"] != module:
        return False
    if "provider_slot_type" in alt and alt["provider_slot_type"] != owner_type:
        return False
    provider = alt.get("provider")
    return provider in (None, "any", "*") or provider == owner or (module and provider == module)


def _assign_points(weapon, effective, installed):
    """Два поиска: по известным точкам и, при неудаче, по возможным.

    Неизвестную точку разрешаем лишь у подходящего неполного поставщика.
    В обоих поисках все выбранные связи должны образовывать ациклический граф.
    """
    # Внутренний None обозначает корпус. Экземпляр игрового слота тоже может
    # называться body, поэтому его ID нельзя использовать как ключ корпуса.
    owners = [(None, None, None, weapon.get("points", []), weapon.get("points_complete", False))]
    requests = []
    for sid, pair in installed.items():
        mid = effective[sid]
        slot = weapon["slots"][sid]
        owners.append((sid, mid, slot["type"], pair.get("provides", []), pair.get("points_complete", False)))
        if pair.get("stock_root") and mid == slot.get("stock_module_id"):
            continue
        requests.extend((sid, group) for group in pair.get("requirements", []))
    if not requests:
        return True, {}, False

    limits = {}
    candidates = []
    explanations = defaultdict(list)
    for consumer, alternatives in requests:
        available = []
        possible = []
        types = []
        for alt in alternatives:
            point_type = alt["type"]
            provider_hint = alt.get("provider_slot") or alt.get("provider_module_id") or alt.get("provider")
            types.append(f"{point_type} ({provider_hint})" if provider_hint else point_type)
            for owner, module, owner_type, points, complete in owners:
                if owner == consumer:
                    continue
                # provider=body строго указывает на корпус, provider_slot=body на модуль.
                if alt.get("provider") == "body" and owner is not None:
                    continue
                public_owner = "body" if owner is None else owner
                if not _provider_matches(alt, public_owner, module, owner_type):
                    continue
                if owner is None and "provider_slot" in alt:
                    continue
                for point in points:
                    if point["type"] != point_type or alt.get("point_id", point["point_id"]) != point["point_id"]:
                        continue
                    key = (owner, point["point_id"])
                    limits[key] = point["capacity"]
                    available.append((owner, key, bool(alt.get("virtual") or point.get("virtual"))))
                if not complete:
                    # При явно указанном ID ёмкость уже известной точки конечна.
                    if alt.get("point_id") and any(p["point_id"] == alt["point_id"] for p in points):
                        continue
                    possible.append((owner, None, True))
        candidates.append((consumer, list(dict.fromkeys(available)), list(dict.fromkeys(possible))))
        explanations[consumer].append(" или ".join(types))

    def solve(allow_unknown):
        choices = [(sid, known + (missing if allow_unknown else [])) for sid, known, missing in candidates]
        choices.sort(key=lambda item: len(item[1]))
        if not all(options for _, options in choices):
            return False
        used = defaultdict(int)
        parents = defaultdict(list)

        def would_cycle(node, target):
            if node == target:
                return True
            return any(would_cycle(parent, target) for parent in parents[node])

        def visit(index):
            if index == len(choices):
                return True
            consumer, options = choices[index]
            for provider, key, virtual in options:
                if not virtual and used[key] >= limits[key]:
                    continue
                if provider is not None and would_cycle(provider, consumer):
                    continue
                if not virtual:
                    used[key] += 1
                if provider is not None:
                    parents[consumer].append(provider)
                if visit(index + 1):
                    return True
                if provider is not None:
                    parents[consumer].pop()
                if not virtual:
                    used[key] -= 1
            return False

        return visit(0)

    if solve(False):
        return True, {}, False
    uncertain = any(missing for _, _, missing in candidates) and solve(True)
    # При отсутствии подходящей точки указываем именно потребителя этой точки.
    lacking = {sid for sid, known, _ in candidates if not known}
    prefix = ("Требуется уточнить точки поставщика" if uncertain
              else "Нет свободной точки с подходящим поставщиком и путём к корпусу")
    reasons = {sid: f"{prefix}: {'; '.join(groups)}"
               for sid, groups in explanations.items() if not lacking or sid in lacking}
    return False, reasons, uncertain


def _catalog_effects(module):
    effects = defaultdict(list)
    unknown = {}
    for effect in module.get("effects", []):
        key = effect["stat_id"]
        if effect.get("state", "known") != "known":
            unknown[key] = effect.get("reason", "Эффект требует проверки")
        else:
            op, value, priority = effect["op"], game_number(effect["value"]), effect.get("priority")
            if key == "fire_rate" and op == "mul" and value > 0:
                # Подтверждённый множитель частоты обратно меняет интервал.
                effects["shot_interval"].append(("mul", 1 / value, priority))
            else:
                effects[key].append((op, value, priority))
    for effect in module.get("unresolved_effects", []):
        for key in effect.get("affected_stats", ["*"]):
            unknown[key] = effect.get("reason", "Неизвестно назначение эффекта") + " · " + _source_note(effect)
    return effects, unknown


def _operations(entries):
    mul, add = Fraction(1), Fraction(0)
    sets = []
    for op, value, priority in entries:
        if op == "mul":
            mul *= value
        elif op == "add":
            add += value
        elif op == "set":
            sets.append((value, priority))
    if not sets:
        return mul, add, None, ""
    if len({value for value, _ in sets}) == 1:
        return mul, add, sets[0][0], ""
    if any(priority is None for _, priority in sets):
        return mul, add, None, "Конфликт set: нужны приоритеты всех операций"
    highest = max(priority for _, priority in sets)
    winners = {value for value, priority in sets if priority == highest}
    if len(winners) > 1:
        return mul, add, None, "Конфликт set: разные значения старшего приоритета"
    return mul, add, winners.pop(), ""


_NEUTRAL = (Fraction(1), Fraction(0), None, "")


def _base_metric(fact, definition, selected, factory, same, problem, factory_problem):
    observation = fact.get("value") if fact.get("state", "known") == "known" else None
    observation = None if observation is None else game_number(observation)
    mul, add, override, conflict = selected
    fm, fa, fset, fconflict = factory
    was_stock = fact.get("basis") == "stock"
    if was_stock:
        stock = observation
        base = None if (stock is None or fset is not None or fconflict or factory_problem or fm == 0) else (stock - fa) / fm
    else:
        base = observation
        stock = fset if fset is not None else (None if base is None else base * fm + fa)
        if fconflict or factory_problem:
            stock = None
    if was_stock and same and stock is not None:
        # Опубликованное наблюдение относится именно к штатной конфигурации.
        return metric_known(stock), (metric_known(0) if stock else metric_missing("Штатное значение равно нулю"))
    if problem or conflict:
        missing = metric_missing(problem or conflict)
        return missing, missing
    if override is not None:
        actual = override
    else:
        actual = None if base is None and mul else (base or 0) * mul + add
    if actual is None:
        reason = fact.get("reason", "Абсолютная база не опубликована")
        absolute = metric_missing(reason, mul=mul, add=add)
    else:
        absolute = metric_known(actual)
    if actual is not None and stock not in (None, 0):
        relative = metric_known(100 * (actual / stock - 1))
    elif (definition.get("positive_base") or fact.get("positive_base")) and stock != 0 and (
        override is None and fset is None and not factory_problem and not fconflict and fm and add == fa == 0
    ):
        relative = metric_known(100 * (mul / fm - 1))
    else:
        relative = metric_missing("Нужна ненулевая штатная база", mul=mul, add=add)
    return absolute, relative


class CandidateContextError(ValueError):
    """Границы сценария зависят от конкретного полного состава."""


def _context_integer(value, name, minimum=0):
    x = game_number(value)
    if x.denominator != 1 or x < minimum:
        raise ValueError(f"{name}: требуется целое число ≥ {minimum}")
    return int(x)


def _context_duration(ctx, key, default=None):
    raw = ctx.get(key, default)
    if raw is None:
        return None
    duration = game_number(raw)
    if duration < 0:
        raise ValueError(f"{key}: длительность должна быть неотрицательной")
    return duration


def _adjust_derived(key, measurement, operations, unknown):
    problem = unknown.get(key) or unknown.get("*")
    if problem:
        return metric_missing(problem)
    if key not in operations or measurement["state"] in ("not_applicable", "unreachable"):
        return measurement
    mul, add, override, error = operations[key]
    if error:
        return metric_missing(error)
    if override is not None:
        return metric_known(override)
    if measurement["value"] is not None:
        return metric_known(measurement["value"] * mul + add)
    return measurement


def _game_scenario(base_metrics, ctx, weapon, operations, unknown):
    """Условная игровая цель и начальное состояние. Все длительности в секундах."""
    def number(key):
        return base_metrics.get(key, {}).get("value")

    out = {}
    d, pellets, t = number("damage"), number("projectiles"), number("shot_interval")
    c, r = number("capacity"), number("reserves")
    h = ctx.get("hits", pellets)
    h = None if h is None else _context_integer(h, "Попадания")
    if pellets is not None:
        pellets = _context_integer(pellets, "Снарядов", 1)
        if h is not None and h > pellets:
            raise CandidateContextError("Число попаданий превышает число снарядов")
    health = game_number(ctx.get("health", 100))
    if health <= 0:
        raise ValueError("Здоровье цели должно быть положительным")
    if t is not None and t <= 0:
        t = None

    def record(key, value, reason="Недостаточно игровых данных"):
        measurement = metric_missing(reason) if value is None else metric_known(value)
        out[key] = _adjust_derived(key, measurement, operations, unknown)
        return out[key]["value"]

    damage = None if d is None or pellets is None else d * pellets
    scenario_damage = 0 if h == 0 else (None if d is None or h is None else d * h)
    damage = record("shot_damage", damage)
    scenario_damage = record("scenario_shot_damage", scenario_damage)
    record("fire_rate", None if t is None else 60 / t)
    record("dps", None if t is None or damage is None else damage / t)
    shots = None if scenario_damage is None or scenario_damage <= 0 else math.ceil(health / scenario_damage)
    shots = record("shots_needed", shots)
    if shots is not None:
        if shots.denominator != 1 or shots <= 0:
            shots = None
            out["shots_needed"] = metric_missing("Ожидалось положительное целое число выстрелов")
        else:
            shots = int(shots)
    if scenario_damage is not None and scenario_damage <= 0:
        out["shots_needed"] = metric_missing("Условная цель не получает урона", "unreachable")
    has_mags = ctx.get("magazine_model", weapon.get("magazine_model", True))
    if c is not None:
        c = _context_integer(c, "Ёмкость", 1)
    r = ctx.get("reserves", r)
    if r is not None:
        r = _context_integer(r, "Полных запасных магазинов")
    current = ctx.get("current_load", c)
    if current is not None:
        current = _context_integer(current, "Текущий заряд")
        if c is not None and current > c:
            raise CandidateContextError("Текущий заряд превышает ёмкость")
    interval = _context_duration(ctx, "magazine_transition", number("reload_time"))
    start = _context_duration(ctx, "start_time")
    pattern = ctx.get("cycle_transitions")
    if pattern is not None:
        if not isinstance(pattern, (list, tuple)) or not pattern:
            raise ValueError("Нужен непустой список длительностей переходов")
        pattern = [None if val is None else game_number(val) for val in pattern]
        if any(val is not None and val < 0 for val in pattern):
            raise ValueError("Длительность перехода должна быть неотрицательной")

    def sequence_time(count, offset=0):
        if count == 0:
            return Fraction(0)
        if pattern is None:
            return None if t is None else count * t
        periods, tail = divmod(count, len(pattern))
        segment = [pattern[(offset + i) % len(pattern)] for i in range(tail)]
        if periods and None in pattern or None in segment:
            return None
        return (periods * sum(pattern) if periods else 0) + sum(segment)

    record("magazine_damage", None if c is None or damage is None else c * damage)
    record("scenario_magazine_damage", None if c is None or scenario_damage is None else c * scenario_damage)
    record("magazine_time", None if c is None else sequence_time(c - 1))
    record("ammo", None if c is None or r is None else c * (1 + r))
    if not has_mags:
        for key in ("magazine_damage", "scenario_magazine_damage", "magazine_time", "ammo"):
            out[key] = metric_missing("Магазинная модель неприменима", "not_applicable")

    if "ammo" in unknown or "ammo" in operations:
        total = out["ammo"]["value"]
        reserve_rounds = None if c is None or total is None else max(0, total - c)
    else:
        reserve_rounds = 0 if r == 0 else (None if c is None or r is None else c * r)
    if out["shots_needed"]["state"] == "unreachable":
        out["ttk"] = out["shots_needed"].copy()
    elif shots is None:
        out["ttk"] = metric_missing("Неизвестен урон по сценарию")
    elif not has_mags:
        record("ttk", sequence_time(shots - 1), "Неизвестен необходимый переход")
    elif current is None:
        out["ttk"] = metric_missing("Неизвестен начальный заряд")
    elif shots > current and reserve_rounds is not None and shots - current > reserve_rounds:
        out["ttk"] = metric_missing("Недостаточный боезапас", "unreachable")
    elif shots > current and (c is None or reserve_rounds is None):
        out["ttk"] = metric_missing("Неизвестен доступный боезапас")
    else:
        # Первый переход для пустого старта является подготовкой до первого выстрела.
        later_shots = max(0, shots - current)
        magazines = 0 if not later_shots else (later_shots + c - 1) // c
        reloads = magazines - int(current == 0)
        preparation = start if current == 0 else Fraction(0)
        # Цикл начинается заново в каждом магазине (ручной/очередной режим).
        first_count = max(0, min(current, shots) - 1)
        partial = sequence_time(first_count)
        full_span = sequence_time(c - 1) if magazines > 1 else 0
        last_span = sequence_time((later_shots - 1) % c) if magazines else 0
        if (preparation is None or (reloads and interval is None)
                or partial is None or full_span is None or last_span is None):
            out["ttk"] = metric_missing("Неизвестен необходимый переход или время подготовки")
        else:
            result = preparation + reloads * (interval or 0) + partial + max(0, magazines - 1) * full_span + last_span
            record("ttk", result)
    return out


class Engine:
    def __init__(self, catalog):
        self.catalog = catalog
        self.pairs = {(p["weapon_id"], p["module_id"]): p for p in catalog["compatibility"].values()}
        self.base_ids = [key for key, info in catalog["stats"].items() if info.get("kind", "base") == "base"]
        self.derived_ids = [key for key in catalog["stats"] if key not in self.base_ids]
        self.effects = {mid: _catalog_effects(mod) for mid, mod in catalog["modules"].items()}
        self.stock = {}
        self.factory_effects = {}
        self._factory_scenarios = {}
        self._stock_installable = {}
        for wid, weap in catalog["weapons"].items():
            self.stock[wid] = {sid: slot.get("stock_module_id") for sid, slot in weap["slots"].items()}
            self.factory_effects[wid] = self._collect(self.stock[wid])

    def _collect(self, effective):
        effects = defaultdict(list)
        problems = {}
        for mid in effective.values():
            if mid in self.effects:
                ops, unknown = self.effects[mid]
                for key, entries in ops.items():
                    effects[key].extend(entries)
                problems.update(unknown)
        return {sid: _operations(ops) for sid, ops in effects.items()}, problems

    def compatibility(self, weapon_id, actions=None):
        actions = actions or {}
        weapon = self.catalog["weapons"].get(weapon_id)
        result = {"status": "compatible", "effective": {}, "reasons": [], "slot_reasons": {}}
        def flag(sid, message, uncertain=False):
            issues = result["slot_reasons"]
            if sid in issues:
                issues[sid] += " · " + message
            else:
                issues[sid] = message
            result["reasons"].append((sid + ": " if sid else "") + message)
            if not uncertain:
                result["status"] = "conflict"
            elif result["status"] == "compatible":
                result["status"] = "unknown"

        if weapon is None:
            flag("", f"Модель отсутствует в ревизии: {weapon_id}", True)
            return result
        slots = weapon["slots"]
        installed = {}
        for sid in actions.keys() - slots.keys():
            flag(sid, "Слот отсутствует в этой ревизии")
        for sid, slot in slots.items():
            mid = selected_module(slot, actions.get(sid, "stock"))
            result["effective"][sid] = mid
            if mid is None:
                if slot["required"]:
                    flag(sid, "Обязательный слот пуст")
                continue
            module = self.catalog["modules"].get(mid)
            if module is None:
                installed[sid] = {}
                flag(sid, f"Деталь {mid} отсутствует в этой ревизии", True)
                continue
            if module["slot_type"] != slot["type"]:
                flag(sid, "Несовместимый тип слота")
            pair = self.pairs.get((weapon_id, mid), {})
            installed[sid] = pair
            if pair.get("mount_unknown") and not (pair.get("stock_root") and mid == slot.get("stock_module_id")):
                flag(sid, pair["mount_unknown"], True)
            allowed_stock = (mid == slot.get("stock_module_id") and
                             (slot.get("stock_confirmed") or slot.get("source_id") or slot.get("stock_source_id")))
            if pair.get("status") == "forbidden":
                flag(sid, pair.get("reason") or "Запрещено источником")
            elif pair.get("status") != "allowed" and not allowed_stock:
                flag(sid, pair.get("reason") or "Нет сведений о допуске по модели", True)
        ids = defaultdict(list)
        for sid, mid in result["effective"].items():
            ids[mid].append(sid)
        conflicts = set()
        for sid, pair in installed.items():
            for excluded in pair.get("excludes", []):
                other = excluded["module_id"] if isinstance(excluded, dict) else excluded
                for other_slot in ids.get(other, []):
                    if other_slot != sid:
                        conflicts.add(tuple(sorted((sid, other_slot))))
        for a, b in sorted(conflicts):
            flag(a, f"Исключает деталь в слоте {b}")
            flag(b, f"Исключает деталь в слоте {a}")
        valid, diagnostics, uncertain = _assign_points(weapon, result["effective"], installed)
        if not valid:
            for sid, message in diagnostics.items():
                flag(sid, message, uncertain)
        return result

    def evaluate(self, weapon_id, actions=None, context=None):
        result = self.compatibility(weapon_id, actions)
        metrics = result["metrics"] = {}
        if result["status"] != "compatible":
            return result
        weapon = self.catalog["weapons"][weapon_id]
        same = result["effective"] == self.stock[weapon_id]
        stock_operations, stock_unknown = self.factory_effects[weapon_id]
        operations, unknown = self.factory_effects[weapon_id] if same else self._collect(result["effective"])
        for key in self.base_ids:
            definition = self.catalog["stats"][key]
            metrics[key], metrics[key + "__rel"] = _base_metric(
                weapon.get("values", {}).get(key, {}), definition,
                operations.get(key, _NEUTRAL), stock_operations.get(key, _NEUTRAL), same,
                unknown.get(key) or unknown.get("*"), stock_unknown.get(key) or stock_unknown.get("*"))
        if weapon_id not in self._stock_installable:
            self._stock_installable[weapon_id] = same or self.compatibility(weapon_id)["status"] == "compatible"
        if not self._stock_installable[weapon_id]:
            for key in self.base_ids:
                metrics[key + "__rel"] = metric_missing("Штатная конфигурация не прошла проверку установки")
        if self.derived_ids:
            ctx = context or {}
            scenario = _game_scenario(metrics, ctx, weapon, operations, unknown)
            for key in self.derived_ids:
                definition = self.catalog["stats"][key]
                formula = definition["formula_id"]
                val = scenario.get(formula, metric_missing(f"Неизвестная формула {formula}"))
                if key != formula:
                    val = _adjust_derived(key, val, operations, unknown)
                metrics[key] = val
            cache_key = (weapon_id, repr(sorted(ctx.items())))
            if len(self._factory_scenarios) >= 128 and cache_key not in self._factory_scenarios:
                self._factory_scenarios.clear()
            stock_metrics = metrics if same else self._factory_scenarios.get(cache_key)
            if stock_metrics is None:
                try:
                    stock_metrics = self.evaluate(weapon_id, {}, ctx)["metrics"]
                except CandidateContextError:
                    # Например, текущий заряд для расширенного магазина выше штатной ёмкости.
                    stock_metrics = {}
                self._factory_scenarios[cache_key] = {k: stock_metrics[k] for k in self.derived_ids if k in stock_metrics}
            if same:
                self._factory_scenarios[cache_key] = {key: metrics[key] for key in self.derived_ids}
            for key in self.derived_ids:
                current = metrics[key]["value"]
                reference = stock_metrics.get(key, {}).get("value")
                metrics[key + "__rel"] = (metric_known(100 * (current / reference - 1))
                    if current is not None and reference not in (None, 0)
                    else metric_missing("Нужна известная ненулевая штатная величина в этом сценарии"))
        return result


def _metric_test_catalog():
    base = {"damage": 25, "projectiles": 1, "shot_interval": 0.1,
            "capacity": 30, "reserves": 2, "index": 10}
    dependency = {"fire_rate": ["shot_interval"], "shot_damage": ["damage", "projectiles"],
                  "scenario_shot_damage": ["damage", "projectiles"], "dps": ["shot_damage", "shot_interval"],
                  "shots_needed": ["scenario_shot_damage"], "ttk": ["shots_needed", "shot_interval", "capacity"],
                  "magazine_damage": ["capacity", "shot_damage"],
                  "scenario_magazine_damage": ["capacity", "scenario_shot_damage"],
                  "magazine_time": ["capacity", "shot_interval"], "ammo": ["capacity", "reserves"]}
    stats = {key: dict(name=key, meaning=key, unit="pt", direction="max", precision=2, kind="base")
             for key in base}
    for key, deps in dependency.items():
        stats[key] = dict(name=key, meaning=key, unit="pt", direction="max", precision=2, kind="derived",
                          formula_id=key, dependencies=deps)
    return {"schema_version": 1, "game_version": "demo", "revision": 1,
            "catalog_date": "2026-11-23", "calc_version": 1,
            "sources": {"s": {"name": "Эталон", "version": "1", "locator": "self-test"}},
            "stats": stats, "slots": {"part": {"name": "Тест", "order": 1}},
            "modules": {}, "compatibility": {},
            "weapons": {"demo": {"name": "Демо", "class": "Тест", "source_id": "s",
                                  "points": [], "points_complete": True, "slots": {},
                                  "values": {key: {"value": value, "basis": "base", "source_id": "s"}
                                             for key, value in base.items()}}}}


def _test_part(catalog, sid, mid, needs=(), provides=(), stock=False, effects=()):
    weapon = catalog["weapons"]["demo"]
    weapon["slots"].setdefault(sid, {"type": "part", "required": False,
                                      "order": len(weapon["slots"]), "stock_module_id": None})
    if stock:
        weapon["slots"][sid]["stock_module_id"] = mid
    catalog["modules"][mid] = {"name": mid, "slot_type": "part", "source_id": "s", "effects": list(effects)}
    catalog["compatibility"][mid] = dict(weapon_id="demo", module_id=mid, status="allowed", reason="Тест",
                                          source_id="s", points_complete=True, requirements=list(needs),
                                          provides=list(provides), excludes=[])


def _test_core_points():
    cat = _metric_test_catalog()
    w = cat["weapons"]["demo"]
    w["points"] = [{"type": "socket", "point_id": "one", "capacity": 1}]
    _test_part(cat, "a", "a1", [[{"type": "socket", "provider": "body"}]], stock=True)
    _test_part(cat, "b", "b1", [[{"type": "socket", "provider": "body", "point_id": "one"}]], stock=True)
    assert Engine(cat).compatibility("demo")["status"] == "conflict"
    w["points"].append({"type": "socket", "point_id": "two", "capacity": 1})
    assert Engine(cat).compatibility("demo")["status"] == "compatible"
    a, b = cat["compatibility"]["a1"], cat["compatibility"]["b1"]
    a["requirements"] = [[{"type": "b", "provider_module_id": "b1"}]]
    b["provides"] = [{"type": "b", "point_id": "pb", "capacity": 1}]
    b["requirements"] = [[{"type": "a", "provider_module_id": "a1"}]]
    a["provides"] = [{"type": "a", "point_id": "pa", "capacity": 1}]
    assert Engine(cat).compatibility("demo")["status"] == "conflict"
    a["requirements"][0].append({"type": "socket", "provider": "body"})
    assert Engine(cat).compatibility("demo")["status"] == "compatible"
    b["requirements"] = [[{"type": "missing", "provider_slot": "a"}]]
    a["points_complete"] = False
    assert Engine(cat).compatibility("demo")["status"] == "unknown"
    b["excludes"] = [{"module_id": "a1", "source_id": "s"}]
    both = Engine(cat).compatibility("demo")
    assert both["status"] == "conflict" and {"a", "b"} == set(both["slot_reasons"])


def _test_core_ops():
    cat = _metric_test_catalog()
    weapon = cat["weapons"]["demo"]
    def effect(key, op, value, **extras):
        return dict(stat_id=key, op=op, value=value, state="known", source_id="s", **extras)

    _test_part(cat, "s", "m")
    module = cat["modules"]["m"]
    def selected(ops, key="index"):
        module["effects"] = ops
        return Engine(cat).evaluate("demo", {"s": "m"})["metrics"][key]
    assert selected([effect("index", "mul", 2), effect("index", "add", 3)])["value"] == 23
    ties = [effect("index", "set", 15, priority=1), effect("index", "set", 20, priority=2)]
    assert selected(ties)["value"] == 20
    ties[0]["priority"] = 2
    assert selected(ties)["state"] == "unknown"
    ties[0]["value"] = 20
    assert selected(ties)["value"] == 20
    weapon["values"]["shot_interval"]["value"] = 0.12
    module["effects"] = [effect("fire_rate", "mul", 1.5)]
    metrics = Engine(cat).evaluate("demo", {"s": "m"})["metrics"]
    assert metrics["shot_interval"]["value"] == Fraction(8, 100) and metrics["fire_rate"]["value"] == 750
    module["unresolved_effects"] = [dict(affected_stats=["fire_rate", "shot_interval"],
        reason="Направление не подтверждено", locator="Синтетика", source_id="s")]
    metrics = Engine(cat).evaluate("demo", {"s": "m"})["metrics"]
    assert all(metrics[key]["state"] == "unknown" for key in ("shot_interval", "fire_rate", "ttk", "dps"))
    assert metrics["capacity"]["value"] == 30
    module["unresolved_effects"] = []
    assert selected([effect("shot_damage", "add", 5)], "dps")["value"] == 250
    module["unresolved_effects"] = [dict(affected_stats=["shot_damage"], reason="Спорный урон", source_id="s")]
    independent = Engine(cat).evaluate("demo", {"s": "m"})["metrics"]
    assert independent["dps"]["state"] == "unknown" and independent["scenario_shot_damage"]["value"] == 25
    module["unresolved_effects"] = []
    _test_part(cat, "s", "factory", stock=True, effects=[effect("index", "mul", 1.2)])
    weapon["values"]["index"] = {"value": None, "basis": "base", "state": "unknown", "positive_base": True}
    assert selected([effect("index", "mul", 1.5)], "index__rel")["value"] == 25
    mixed = [effect("index", "mul", 1.2), effect("index", "add", 5)]
    val = selected(mixed)
    assert (val["state"], val["mul"], val["add"]) == ("unknown", Fraction(6, 5), 5)
    assert selected(mixed, "index__rel")["state"] == "unknown"
    weapon["values"]["index"] = {"value": 100, "basis": "stock", "source_id": "s"}
    cat["modules"]["factory"]["effects"] = [effect("index", "mul", 1.25)]
    assert selected([])["value"] == 80
    cat["modules"]["factory"]["effects"] = [effect("index", "set", 100)]
    assert selected([])["state"] == "unknown"
    assert Engine(cat).evaluate("demo")["metrics"]["index"]["value"] == 100


def _test_core_scenario():
    cat = _metric_test_catalog()
    values = cat["weapons"]["demo"]["values"]
    def run(**context):
        return Engine(cat).evaluate("demo", {}, context)["metrics"]
    x = run()
    names = ("fire_rate", "dps", "shots_needed", "ttk", "magazine_damage", "magazine_time", "ammo")
    assert [x[key]["value"] for key in names] == [600, 250, 4, Fraction("0.3"), 750, Fraction("2.9"), 90]
    values["shot_interval"]["value"] = None
    x = run()
    assert all(x[key]["state"] == "unknown" for key in ("fire_rate", "dps", "ttk"))
    assert [x[key]["value"] for key in ("damage", "capacity", "shots_needed")] == [25, 30, 4]
    values["capacity"]["value"] = 1
    x = run(health=25)
    assert x["ttk"]["value"] == x["magazine_time"]["value"] == 0
    assert run(health=25, current_load=0, reserves=1, start_time=0.8)["ttk"]["value"] == Fraction("0.8")
    values["capacity"]["value"] = 2
    values["shot_interval"]["value"] = 0.1
    x = run(reserves=1, magazine_transition=1.2)
    assert x["ttk"]["value"] == Fraction("1.4")
    assert run(reserves=0)["ttk"]["state"] == "unreachable"
    x = run(reserves=1)
    assert x["ttk"]["state"] == "unknown" and x["shots_needed"]["value"] == 4
    values["capacity"]["value"] = 3
    assert run(health=75, cycle_transitions=[0.1, 0.5])["ttk"]["value"] == Fraction("0.6")
    assert run(health=75, magazine_model=False)["ammo"]["state"] == "not_applicable"
    values["damage"]["value"], values["projectiles"]["value"], values["shot_interval"]["value"] = 10, 4, 0.2
    x = run(health=50, hits=2)
    names = ("shot_damage", "scenario_shot_damage", "dps", "scenario_magazine_damage", "ttk")
    assert [x[key]["value"] for key in names] == [40, 20, 200, 60, Fraction("0.4")]
    assert run(hits=0)["ttk"]["state"] == "unreachable"
    try:
        run(hits=5)
    except ValueError:
        pass
    else:
        raise AssertionError("Ч5: ожидался отказ h>P")


def tests_core(catalog=None):
    from copy import deepcopy
    _test_core_points()
    _test_core_ops()
    _test_core_scenario()
    evidence = {
        "С2": "Одна точка на двоих — конфликт, две — совместимо. Цикл отклонён; альтернатива к корпусу разрешена; неполный поставщик — требует проверки.",
        "Ч2": "0,12 / 1,5 = 0,08 с и 750 выстр./мин; спорная частота блокирована. 10×2+3=23, старший set=20, конфликт равных приоритетов. Чистые множители +25%; 1,2B+5 разделено.",
        "Ч3": "600; 250; 4; 0,3; 750; 2,9; 90. Без интервала сохраняются 25 урона, 30 ёмкости и 4 выстрела.",
        "Ч4": "Один готовый выстрел 0 с, из пустого старта 0,8 с. Смена магазина 1,4 с, R=0 недостижимо; особый цикл 0,6 с.",
        "Ч5": "40; 20; 200; 60; 0,4 с. Ноль попаданий — недостижимо, h>P — ошибка."
    }
    if catalog is not None:
        core = Engine(catalog)
        for wid in ("sg5_k", "sg5_kf"):
            actions = {"forearm": "m91", "underbarrel": "m176"}
            assert core.compatibility(wid, actions)["status"] == "compatible", wid
            actions["forearm"] = "m94"
            bad = core.evaluate(wid, actions)
            assert bad["status"] == "conflict" and "underbarrel" in bad["slot_reasons"] and not bad["metrics"], bad
            changed = deepcopy(catalog)
            changed["weapons"][wid]["points"].append({"point_id": "test_unrelated",
                                                     "type": "underbarrel", "capacity": 2})
            assert Engine(changed).compatibility(wid, actions)["status"] == "conflict"
            actions["underbarrel"] = "empty"
            assert core.compatibility(wid, actions)["status"] == "compatible"
        actions = {"forearm": "m94", "muzzle": "m116", "magazine": "m428", "underbarrel": "empty"}
        result = core.evaluate("sg5_k", actions)
        assert result["status"] == "compatible", result["reasons"]
        x = result["metrics"]
        assert [x[key]["value"] for key in ("ads_speed__rel", "capacity")] == [50, 15]
        assert x["ads_time"]["state"] == x["reload_time"]["state"] == "unknown"
        assert core.evaluate("sg5_k")["metrics"]["ads_speed__rel"]["value"] == 0
        evidence["С1"] = "Обе игровые модели: SG5-K + Shifter совместимы; Tropic оставляет Shifter в конфликте, чужая точка на корпусе его не снимает; снятие Shifter устраняет конфликт."
        evidence["Ч1"] = "Книга 0.9.1: штатный ADS +0%, Tropic/Hartos/Curved 15 дают +50%, ёмкость 15, абсолютное время ADS и время перезарядки неизвестны. S=100 / 1,25 даёт B=80."
    return evidence


# Поиск по подтверждённым игровым данным.
from bisect import bisect_left
from copy import deepcopy
from fractions import Fraction
from itertools import combinations, islice, product
from math import prod
from threading import Event
from time import monotonic


class SearchRequests:
    """Вызывается потоком интерфейса при старте, смене документа и приёме событий."""
    def __init__(self):
        self.token = 0
        self.cancel = Event()

    def new(self):
        self.cancel.set()
        self.token += 1
        self.cancel = Event()
        return self.token, self.cancel

    def current(self, token):
        return token == self.token


def _search_number(value, path):
    try:
        if isinstance(value, bool):
            raise ValueError
        return Fraction(str(value).strip().replace(",", "."))
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError(f"{path} — требуется конечное число, получено {value!r}") from None


def _search_units(catalog, entry, path):
    stat = entry.get("stat")
    if not isinstance(stat, str) or not stat:
        raise ValueError(f"{path}.stat — укажите показатель")
    relative = stat.endswith("__rel")
    base = stat.removesuffix("__rel")
    spec = catalog["stats"].get(base)
    if spec is None:
        raise ValueError(f"{path}.stat — неизвестный показатель {stat}")
    native = "%" if relative else spec["unit"]
    unit = entry.get("unit", native)
    if unit == native:
        factor = 1
    else:
        seconds = {"s": 1, "с": 1, "ms": 1000, "мс": 1000}
        if native not in seconds or unit not in seconds:
            raise ValueError(f"{path}.unit — несопоставимые единицы {native} и {unit}")
        factor = Fraction(seconds[unit], seconds[native])
    return spec, unit, factor, relative


def _search_query(catalog, query):
    q = deepcopy(query)
    wid = q.get("weapon_id")
    if wid not in catalog["weapons"]:
        raise ValueError("weapon_id — выберите существующую модель")
    for key in ("game_version", "revision", "calc_version"):
        if key in q and q[key] != catalog[key]:
            raise ValueError(f"{key} — запрос относится к другому каталогу или расчёту")
        q[key] = catalog[key]
    q.setdefault("profile", "published")
    if q["profile"] not in ("published", "По опубликованным эффектам"):
        raise ValueError("profile — неизвестный профиль расчёта")
    q.setdefault("context", {})
    q.setdefault("thresholds", [])
    q.setdefault("locks", {})
    q.setdefault("mode", "single")
    q.setdefault("k", 5)
    if type(q["k"]) is not int or not 1 <= q["k"] <= 20:
        raise ValueError("K — целое число от 1 до 20")
    if q["mode"] not in ("single", "weighted"):
        raise ValueError("mode — выберите одиночный или взвешенный поиск")
    weighted = q["mode"] == "weighted"
    criteria = q.get("criteria")
    if not isinstance(criteria, list) or not criteria or (not weighted and len(criteria) != 1):
        raise ValueError("criteria — в одиночном режиме нужна одна цель, во взвешенном минимум две")
    compiled = []
    seen = set()
    for i, item in enumerate(criteria):
        path = f"criteria[{i}]"
        spec, unit, factor, relative = _search_units(catalog, item, path)
        stat = item["stat"]
        if stat in seen:
            raise ValueError(f"{path}.stat — повтор показателя {stat}")
        seen.add(stat)
        direction = item.setdefault("direction", spec["direction"])
        if spec["direction"] == "display_only" or direction not in ("max", "min"):
            raise ValueError(f"{path}.direction — показатель должен иметь направление max/min")
        weight = _search_number(item.setdefault("weight", 1), path + ".weight")
        if weight < 0:
            raise ValueError(f"{path}.weight — отрицательный вес")
        item["unit"] = unit
        low = high = None
        if weighted and weight > 0:
            scale = {} if relative else spec.get("scale", {})
            for key in ("low", "high"):
                if item.get(key) is None:
                    if key not in scale:
                        raise ValueError(f"{path}.{key} — введите ориентир для {stat} или уберите критерий")
                    item[key] = str(_search_number(scale[key], path + "." + key) * factor)
            low = _search_number(item["low"], path + ".low")
            high = _search_number(item["high"], path + ".high")
            if high <= low:
                raise ValueError(f"{path} — шкала требует low < high")
        if weight > 0:
            compiled.append((stat, direction, unit, factor, weight, low, high))
    if not compiled:
        raise ValueError("criteria — нужен хотя бы один положительный вес")
    if weighted and len(compiled) < 2:
        raise ValueError("criteria — для одной положительной цели выберите одиночный режим")

    if not isinstance(q["context"], dict) or not isinstance(q["locks"], dict):
        raise ValueError("context и locks должны быть объектами")
    if q["context"]:
        _game_scenario({}, q["context"], catalog["weapons"][wid], {}, {})
    slots = catalog["weapons"][wid]["slots"]
    for slot_id, choice in q["locks"].items():
        if slot_id not in slots:
            raise ValueError(f"locks.{slot_id} — слот отсутствует у модели")
        if isinstance(choice, dict):
            action = choice.get("action")
            if action not in ("explicit", "stock", "empty"):
                raise ValueError(f"locks.{slot_id}.action — неизвестное действие")
            mid = choice.get("module_id") if action == "explicit" else action
        else:
            mid = choice
        if mid not in ("empty", "stock"):
            if not isinstance(mid, str) or mid not in catalog["modules"]:
                raise ValueError(f"locks.{slot_id} — закреплённая деталь отсутствует")
            if catalog["modules"][mid]["slot_type"] != slots[slot_id]["type"]:
                raise ValueError(f"locks.{slot_id} — деталь другого типа")

    # Объединение порогов одной величины в исходной единице до запуска.
    bounds = {}
    for i, rule in enumerate(q["thresholds"]):
        path = f"thresholds[{i}]"
        _, unit, factor, _ = _search_units(catalog, rule, path)
        rule["unit"] = unit
        stat = rule["stat"]
        limits = bounds.setdefault(stat, {})
        if all(rule.get(key) is None for key in ("min", "max")):
            raise ValueError(f"{path} — задайте хотя бы одну границу")
        for key in ("min", "max"):
            if rule.get(key) is None:
                continue
            value = _search_number(rule[key], path + "." + key) / factor
            open_bound = rule.get(key + "_open", False)
            if type(open_bound) is not bool:
                raise ValueError(f"{path}.{key}_open — требуется true или false")
            old = limits.get(key)
            if old is None or (value > old[0] if key == "min" else value < old[0]):
                limits[key] = (value, open_bound)
            elif old[0] == value:
                limits[key] = (value, old[1] or open_bound)
    for stat, limits in bounds.items():
        if "min" in limits and "max" in limits:
            (lo, lo_open), (hi, hi_open) = limits["min"], limits["max"]
            if lo > hi or (lo == hi and (lo_open or hi_open)):
                raise ValueError(f"thresholds.{stat} — противоречивые пороги")
    return q, compiled, bounds


def _search_domains(catalog, wid, locks):
    weapon = catalog["weapons"][wid]
    slots = sorted(weapon["slots"], key=lambda s: (weapon["slots"][s]["order"], s))
    # Неопределённый допуск не может образовать подтверждённую установку.
    # Штатная деталь всегда идёт в расчёт собственной веткой (заводской факт).
    allowed = {}
    forbidden = set()
    for pair in catalog["compatibility"].values():
        if pair["weapon_id"] != wid:
            continue
        mid = pair["module_id"]
        if pair["status"] == "allowed":
            allowed.setdefault(catalog["modules"][mid]["slot_type"], []).append(mid)
        elif pair["status"] == "forbidden":
            forbidden.add(mid)
    domains = []
    for slot_id in slots:
        if slot_id in locks:
            domains.append([locks[slot_id]])
            continue
        slot = weapon["slots"][slot_id]
        stock = slot.get("stock_module_id")
        domain = ["stock"] if stock is not None or not slot["required"] else []
        if stock is not None and not slot["required"]:
            domain.append("empty")
        domain.extend(mid for mid in allowed.get(slot["type"], ()) if mid != stock)
        domains.append(domain)
    missing_domain = False
    for s, domain in zip(slots, domains):
        if not domain and any(mid not in forbidden and module["slot_type"] == weapon["slots"][s]["type"]
                              for mid, module in catalog["modules"].items()):
            missing_domain = True
    return slots, domains, missing_domain


def _search_compositions(domains):
    """Сначала близкие к штатной сборки. Каждая комбинация посещается единожды."""
    if any(not domain for domain in domains):
        return
    choices = [domain[0] for domain in domains]
    flexible = [i for i, domain in enumerate(domains) if len(domain) > 1]
    yield tuple(choices)
    for n in range(1, len(flexible) + 1):
        for changed in combinations(flexible, n):
            for replacements in product(*(domains[i][1:] for i in changed)):
                for i, choice in zip(changed, replacements):
                    choices[i] = choice
                yield tuple(choices)
            for i in changed:
                choices[i] = domains[i][0]


def _search_score(metrics, criteria, bounds, weighted, total_weight):
    missing = False
    for stat, limits in bounds.items():
        obs = metrics.get(stat, {})
        if obs.get("state") != "known":
            missing = True
            continue
        x = obs["value"]
        if "min" in limits:
            value, open_bound = limits["min"]
            if x < value or (open_bound and x == value):
                return None, "threshold"
        if "max" in limits:
            value, open_bound = limits["max"]
            if x > value or (open_bound and x == value):
                return None, "threshold"
    if missing:
        return None, "unknown_metric"
    parts = []
    score = 0
    for stat, direction, unit, factor, weight, low, high in criteria:
        obs = metrics.get(stat, {})
        if obs.get("state") != "known":
            return None, "unknown_metric"
        x = obs["value"] * factor
        part = {"stat": stat, "value": x, "unit": unit, "direction": direction}
        if weighted:
            u = (x - low if direction == "max" else high - x) / (high - low)
            share = weight / total_weight
            contribution = 100 * share * u
            part.update(low=low, high=high, u=u, share=share, contribution=contribution,
                        outside=u < 0 or u > 1)
            score += contribution
        else:
            score = x
        parts.append(part)
    return (score, parts), None


def _named_provider_groups(engine, wid, slots, domains):
    """Необходимые условия, явно относящиеся к конкретным слотам-поставщикам."""
    weapon = engine.catalog["weapons"][wid]
    pairs, mids = [], []
    positions = {slot: i for i, slot in enumerate(slots)}
    for sid, domain in zip(slots, domains):
        mids.append([selected_module(weapon["slots"][sid], action) for action in domain])
        pairs.append([engine.pairs.get((wid, mid), {}) for mid in mids[-1]])
    rules = {}
    for i, choices in enumerate(pairs):
        for j, pair in enumerate(choices):
            if pair.get("stock_root") and mids[i][j] == weapon["slots"][slots[i]].get("stock_module_id"):
                continue
            for group in pair.get("requirements", ()):
                if not group or any("provider_slot" not in alt for alt in group):
                    continue
                acceptable = {}
                for alt in group:
                    owner = positions.get(alt["provider_slot"])
                    if owner is None or owner == i:
                        continue
                    providers = acceptable.setdefault(owner, set())
                    for n, source in enumerate(pairs[owner]):
                        if mids[owner][n] is not None and not source.get("points_complete", False):
                            # Неполные сведения о поставщике расширяют релаксацию.
                            providers.add(n)
                        if any(p["type"] == alt["type"] and
                               ("point_id" not in alt or p["point_id"] == alt["point_id"])
                               for p in source.get("provides", ())):
                            providers.add(n)
                rules.setdefault(i, {}).setdefault(j, []).append(acceptable)
    return rules


def _can_have_named_providers(possible, rules):
    for i, requirements in rules.items():
        # Для каждой группы годится любой допустимый поставщик. Здесь игнорируем
        # ёмкости, циклы и дополнительные ограничения. Проверка даёт только необходимое условие.
        def candidate_ok(j):
            for group in requirements.get(j, ()):
                if not any(possible[owner] & allowed for owner, allowed in group.items()):
                    return False
            return True
        if not any(candidate_ok(j) for j in possible[i]):
            return False
    return True


def _search_bound(engine, wid, slots, domains, criteria, weighted, total_weight, tick):
    """Внешняя оценка балла по базовым величинам без предположений о совместимости.

    Перебираем различные наборы операций над одной величиной. Проверка точек,
    фиксации остальных величин и пороги могут лишь удалить варианты из этого
    множества. Для взвешенного балла сумма отдельных максимумов — верхняя грань.
    """
    weapon = engine.catalog["weapons"][wid]
    stock_ops, stock_missing = engine.factory_effects[wid]
    named_rules = _named_provider_groups(engine, wid, slots, domains)
    full_domain = [set(range(len(d))) for d in domains]
    bound = 0
    preferred = [set(range(len(domain))) for domain in domains]
    for stat, direction, unit, factor, weight, low, high in criteria:
        key = stat.removesuffix("__rel")
        definition = engine.catalog["stats"][key]
        if definition.get("kind", "base") != "base":
            return None, False, domains
        fact = weapon.get("values", {}).get(key, {})
        signatures = []
        active_slots = []
        n = 1
        for i, (sid, domain) in enumerate(zip(slots, domains)):
            by_choice = {}
            for j, action in enumerate(domain):
                mid = selected_module(weapon["slots"][sid], action)
                ops, _missing = engine.effects.get(mid, ({}, {}))
                # Спорная операция исключит конкретный изменённый состав при полной
                # оценке. В релаксации оставляем опубликованные операции этого модуля.
                by_choice[j] = tuple(ops.get(key, ()))
            unique = set(by_choice.values())
            # У пустого набора нет операций при любом выборе в этом слоте.
            if unique == {()}:
                continue
            n *= len(unique)
            if n > 2000:
                return None, False, domains
            signatures.append(unique)
            by_signature = {}
            for j, sig in by_choice.items():
                by_signature.setdefault(sig, set()).add(j)
            active_slots.append((i, by_choice, by_signature))
        best = None
        some_installation = False
        leaders = []
        for seq, groups in enumerate(product(*signatures)):
            if seq % 32 == 0:
                tick()
            if named_rules:
                possible = list(full_domain)
                for (i, _, by_signature), sig in zip(active_slots, groups):
                    possible[i] = by_signature[sig]
                if not _can_have_named_providers(possible, named_rules):
                    continue
            some_installation = True
            ops = _operations(op for group in groups for op in group)
            # Штатное наблюдение и расчёт изменённого состава допускают
            # разные правила восстановления базы. Оба входят во внешнюю оценку.
            for same in (False, True):
                absolute, relative = _base_metric(
                    fact, definition, ops,
                    stock_ops.get(key, _NEUTRAL), same, None, stock_missing.get(key) or stock_missing.get("*"))
                metric = relative if stat.endswith("__rel") else absolute
                if metric["state"] != "known":
                    continue
                value = metric["value"] * factor
                if best is None or (value > best if direction == "max" else value < best):
                    best = value
                    leaders = [{group} for group in groups]
                elif value == best:
                    for group_set, group in zip(leaders, groups):
                        group_set.add(group)
        if best is None:
            return None, "unknown_objective" if some_installation else "no_installation", domains
        factory = stock_ops.get(key, _NEUTRAL)
        problem = stock_missing.get(key) or stock_missing.get("*")
        stock_metrics = _base_metric(fact, definition, factory, factory, True, problem, problem)
        observation = stock_metrics[int(stat.endswith("__rel"))]
        stock_at_bound = observation["state"] == "known" and observation["value"] * factor == best
        for (i, by_choice, _), leader_ops in zip(active_slots, leaders):
            admissible = {j for j, sig in by_choice.items() if sig in leader_ops}
            if stock_at_bound:
                slot = weapon["slots"][slots[i]]
                admissible.update(j for j, a in enumerate(domains[i])
                                  if selected_module(slot, a) == slot.get("stock_module_id"))
            preferred[i].intersection_update(admissible)
        if weighted:
            u = (best - low if direction == "max" else high - best) / (high - low)
            bound += 100 * weight * u / total_weight
        else:
            bound = best
    restricted = [[a for j, a in enumerate(d) if j in allowed] for d, allowed in zip(domains, preferred)]
    return bound, False, restricted


def _search_tie_order(weapon, slots, domains):
    """Перебор в порядке точных дополнительных ключей: число замен, затем ID."""
    ordered = []
    minimum, maximum = [0] * (len(slots) + 1), [0] * (len(slots) + 1)
    for sid, domain in zip(slots, domains):
        slot = weapon["slots"][sid]
        choices = []
        for action in domain:
            mid = selected_module(slot, action)
            choices.append((mid or "", int(mid != slot.get("stock_module_id")), action))
        ordered.append(sorted(choices, key=lambda item: item[0]))
    for i in reversed(range(len(slots))):
        costs = [entry[1] for entry in ordered[i]]
        if not costs:
            return
        minimum[i] = minimum[i + 1] + min(costs)
        maximum[i] = maximum[i + 1] + max(costs)
    action_list = [None] * len(slots)

    def visit(i, remaining):
        if i == len(slots):
            yield tuple(action_list)
            return
        for _, cost, action in ordered[i]:
            rest = remaining - cost
            if minimum[i + 1] <= rest <= maximum[i + 1]:
                action_list[i] = action
                yield from visit(i + 1, rest)

    for changes in range(minimum[0], maximum[0] + 1):
        yield from visit(0, changes)


def _search_near_bound(weapon, slots, domains, preferred):
    """Сначала проверяем варианты, способные достигнуть границы по всем целям.

    Каждый вариант с граничным баллом входит в preferred, где перебор идёт по
    двум правилам ничьей. Если граница недостижима, возвращаемся к полному
    пространству. Короткий префикс гарантирует раннюю проверку и других вариантов.
    """
    ordered = _search_tie_order(weapon, slots, domains)
    if all(len(a) == len(b) for a, b in zip(domains, preferred)):
        yield from ordered
        return
    flexible = [i for i, d in enumerate(domains) if len(d) > 1]
    seen = set()
    for choice in islice(_search_tie_order(weapon, slots, preferred), 512):
        seen.add(tuple(choice[i] for i in flexible))
        yield choice
    for choice in ordered:
        if seen:
            key = tuple(choice[i] for i in flexible)
            if key in seen:
                seen.remove(key)
                continue
        yield choice


def run_search(engine, query, cancel=None, progress=None):
    """Полный топ-K либо прерываемый префикс перебора. progress получает отдельный отчёт."""
    catalog = engine.catalog
    q, criteria, bounds = _search_query(catalog, query)
    wid = q["weapon_id"]
    slots, domains, missing_domain = _search_domains(catalog, wid, q["locks"])
    total = prod(map(len, domains))
    stocks = [catalog["weapons"][wid]["slots"][s].get("stock_module_id") for s in slots]
    weighted = q["mode"] == "weighted"
    ascending = not weighted and criteria[0][1] == "min"
    total_weight = sum(c[4] for c in criteria)
    keys, best = [], []
    counts = {"threshold": 0, "unknown_metric": 0, "unknown": 0, "conflict": 0, "scenario": 0}
    evaluated = 0
    proof = "exhaustive"
    reasons = []
    started = monotonic()

    def snapshot(outcome, complete=False):
        return {"rows": list(best), "evaluated": evaluated, "total": total,
                "cancelled": outcome == "cancelled", "complete": complete, "outcome": outcome,
                "proof": proof if complete else None, "pruned": total - evaluated if complete else 0,
                "excluded": dict(counts), "reasons": list(reasons), "query": q,
                "elapsed": monotonic() - started}

    if progress is not None:
        progress(snapshot("running"))
    next_progress = started + 0.25
    if cancel is not None and cancel.is_set():
        return snapshot("cancelled")
    optimum = None
    preferred = domains
    if total > 10000:
        def tick():
            nonlocal next_progress
            if cancel is not None and cancel.is_set():
                raise InterruptedError
            if progress is not None and monotonic() >= next_progress:
                progress(snapshot("running"))
                next_progress = monotonic() + 0.25

        try:
            optimum, impossible, preferred = _search_bound(engine, wid, slots, domains, criteria, weighted, total_weight, tick)
        except InterruptedError:
            return snapshot("cancelled")
        if impossible:
            proof = impossible
            if proof == "no_installation":
                reasons.append("Во всех вариантах отсутствует обязательная точка на указанном поставщике")
                return snapshot("no_candidates", complete=True)
            reasons.append("Даже при ослабленных ограничениях установки цель не получает известного значения")
            return snapshot("missing_data", complete=True)
    choices = (_search_compositions(domains) if optimum is None else
               _search_near_bound(catalog["weapons"][wid], slots, domains, preferred))
    for choice in choices:
        if cancel is not None and cancel.is_set():
            return snapshot("cancelled")
        actions = dict(zip(slots, choice))
        try:
            result = engine.evaluate(wid, actions, q["context"])
        except CandidateContextError as error:
            result = {"status": "scenario", "reasons": [str(error)]}
        evaluated += 1
        status = result["status"]
        if status == "compatible":
            scored, excluded = _search_score(result["metrics"], criteria, bounds, weighted, total_weight)
            if excluded:
                counts[excluded] += 1
            else:
                score, parts = scored
                effective = result["effective"]
                ids = tuple(effective.get(s) or "" for s in slots)
                changes = sum(mid != (stock or "") for mid, stock in zip(ids, stocks))
                key = (score if ascending else -score, changes, ids)
                pos = bisect_left(keys, key)
                if pos < q["k"]:
                    keys.insert(pos, key)
                    best.insert(pos, {"actions": actions, "effective": effective, "value": score,
                                      "components": parts, "changes": changes})
                    if len(keys) > q["k"]:
                        keys.pop()
                        best.pop()
        else:
            counts[status] += 1
            if len(reasons) < 4:
                for reason in result.get("reasons", ()):
                    if reason not in reasons:
                        reasons.append(reason)
                        if len(reasons) == 4:
                            break
        if optimum is not None and len(best) == q["k"] and best[-1]["value"] == optimum:
            # Все K уже на границе. При равном балле у оставшихся ключ строго хуже.
            proof = "objective_bound"
            break
        if progress is not None and monotonic() >= next_progress:
            progress(snapshot("running"))
            next_progress = monotonic() + 0.25
    if evaluated == 0 and cancel is not None and cancel.is_set():
        return snapshot("cancelled")
    if best:
        outcome = "results"
    elif counts["unknown"] or counts["unknown_metric"] or missing_domain:
        outcome = "missing_data"
    else:
        outcome = "no_candidates"
    return snapshot(outcome, complete=True)


def _search_fixture(stat_defs, slot_defs):
    """Малый синтетический каталог без связи с игровым импортом."""
    cat = {"schema_version": 1, "game_version": "demo", "revision": 1, "calc_version": 1,
           "catalog_date": "2026-11-23",
           "sources": {"s": {"name": "Синтетическая проверка", "version": "1", "locator": "ТЗ"}},
           "stats": {}, "slots": {}, "weapons": {}, "modules": {}, "compatibility": {}}
    w = {"class": "Тест", "name": "Демо", "source_id": "s", "points_complete": True,
         "points": [], "slots": {}, "values": {}}
    cat["weapons"]["demo"] = w
    for sid, (base, direction, unit) in stat_defs.items():
        cat["stats"][sid] = {"name": sid, "meaning": "Тестовая величина", "kind": "base",
                             "direction": direction, "unit": unit, "precision": 2}
        w["values"][sid] = {"source_id": "s", "basis": "base", "state": "known" if base is not None
                            else "unknown", "value": base}
    for order, (slot, (required, stock, variants)) in enumerate(slot_defs.items()):
        cat["slots"][slot] = {"name": slot, "order": order}
        w["slots"][slot] = {"type": slot, "required": required, "order": order,
                            "stock_module_id": stock}
        for mid, operations in variants.items():
            effects = [{"stat_id": stat, "op": "add", "value": val, "state": "known",
                        "source_id": "s"} for stat, val in operations.items()]
            cat["modules"][mid] = {"name": mid, "slot_type": slot, "effects": effects,
                                   "source_id": "s", "external_id": mid}
            cat["compatibility"]["demo_" + mid] = {
                "weapon_id": "demo", "module_id": mid, "status": "allowed",
                "reason": "Синтетический допуск", "source_id": "s", "points_complete": True,
                "requirements": [], "provides": [], "excludes": []}
    return cat


def _rank_fixture():
    # Обходим p3 раньше p2, чтобы проверить вытеснение равного кандидата на границе K.
    cat = _search_fixture({"index": (10, "max", "pt"), "unreported": (None, "min", "pt")},
                          {"p": (True, "p0", {"p3": {"index": 2}, "p2": {"index": 2},
                                              "p1": {"index": -1}, "p0": {}}),
                           "q": (False, None, {"q5": {"index": 5}})})
    cat["compatibility"]["demo_p1"]["provides"] = [{"point_id": "pin", "type": "port", "capacity": 1}]
    cat["compatibility"]["demo_q5"]["requirements"] = [[{"type": "port", "provider_module_id": "p1"}]]
    return cat


def _weighted_fixture():
    xy = {"A": (12, 5), "B": (10, 3), "C": (9, 6), "D": (11, 4), "E": (11, 4)}
    cat = _search_fixture({"x": (0, "max", "s"), "y": (0, "min", "pt"), "z": (None, "min", "pt")},
                          {"m": (True, None, {name: {"x": x, "y": y} for name, (x, y) in xy.items()})})
    cat["stats"]["x"]["scale"] = {"low": 8, "high": 12}
    cat["stats"]["y"]["scale"] = {"low": 2, "high": 6}
    return cat


def _check_search_r1():
    cat = _rank_fixture()
    engine = Engine(cat)
    max_order = [("p1", "q5", 14), ("p2", "", 12), ("p3", "", 12),
                 ("p0", "", 10), ("p1", "", 9)]
    min_order = [max_order[i] for i in (4, 3, 1, 2, 0)]
    for direction, expected in (("max", max_order), ("min", min_order)):
        # Независимый полный перебор без вызовов Engine, run_search и их сортировки.
        raw = []
        for p in ("p3", "p2", "p1", "p0"):
            for qq in ("q5", ""):
                if qq and p != "p1":
                    continue
                val = 10 + {"p0": 0, "p1": -1, "p2": 2, "p3": 2}[p] + (5 if qq else 0)
                raw.append((p, qq, val))
        raw.sort(key=lambda row: ((-1 if direction == "max" else 1) * row[2],
                                  int(row[0] != "p0") + int(bool(row[1])), row[0], row[1]))
        assert raw == expected, (direction, raw)
        for k in (1, 2, 5, 20):
            result = run_search(engine, {"weapon_id": "demo", "k": k,
                                "criteria": [{"stat": "index", "direction": direction}]})
            observed = [(r["effective"]["p"], r["effective"]["q"] or "", r["value"])
                        for r in result["rows"]]
            assert observed == raw[:k] and result["complete"] and result["evaluated"] == 8, observed
    cat["compatibility"] = dict(reversed(list(cat["compatibility"].items())))
    cat["weapons"]["demo"]["slots"] = dict(reversed(list(cat["weapons"]["demo"]["slots"].items())))
    got = run_search(Engine(cat), {"weapon_id": "demo", "k": 2, "criteria": [{"stat": "index"}]})
    assert [r["effective"]["p"] for r in got["rows"]] == ["p1", "p2"]
    return "Перебор 8 полных составов. Известные 5 мест max 14,12,12,10,9 и min 9,10,12,12,14. Эталоны и все K=1,2,5,20 совпали, включая ничью p2/p3 и перестановку JSON."


def _check_search_r2():
    engine = Engine(_weighted_fixture())

    def query(x, y, k=5):
        return {"weapon_id": "demo", "mode": "weighted", "k": k,
                "criteria": [{"stat": "x", "weight": x}, {"stat": "y", "weight": y}]}

    table = [((70, 30), "ADEBC", [77.5, 67.5, 67.5, 57.5, 17.5]),
             ((20, 80), "BDEAC", [70, 55, 55, 40, 5]),
             ((50, 50), "ABDEC", [62.5, 62.5, 62.5, 62.5, 12.5])]
    xy = {"A": (12, 5), "B": (10, 3), "C": (9, 6), "D": (11, 4), "E": (11, 4)}
    for (wx, wy), names, scores in table:
        independent = {n: Fraction(wx * (x - 8) + wy * (6 - y), 4)
                       for n, (x, y) in xy.items()}
        assert sorted(independent, key=lambda n: (-independent[n], n)) == list(names)
        for k in (2, 5):
            got = run_search(engine, query(wx, wy, k))["rows"]
            assert [r["effective"]["m"] for r in got] == list(names[:k])
            assert [r["value"] for r in got] == scores[:k]
            assert [r["value"] for r in got] == [independent[n] for n in names[:k]]
    original = run_search(engine, query(70, 30))["rows"]
    assert [c["contribution"] for c in original[0]["components"]] == [70, 7.5]
    expected = [(r["effective"], r["value"]) for r in original]
    converted = query(70, 30)
    converted["criteria"][0].update(unit="ms", low=8000, high=12000)
    zero = query(70, 30)
    zero["criteria"].append({"stat": "z", "weight": 0})
    for alternative in [converted, query(7, 3), zero]:
        got = run_search(engine, alternative)["rows"]
        assert [(r["effective"], r["value"]) for r in got] == expected
    assert run_search(engine, converted)["rows"][0]["components"][0]["value"] == 12000
    for name, (x, y) in xy.items():
        locked = query(70, 30)
        locked["locks"] = {"m": name}
        assert run_search(engine, locked)["rows"][0]["value"] == dict(zip("ADEBC", table[0][2]))[name]
    with_limit = query(70, 30)
    with_limit["thresholds"] = [{"stat": "y", "max": 4}]
    assert [r["effective"]["m"] for r in run_search(engine, with_limit)["rows"]] == list("DEB")
    with_limit["criteria"][0].update(low=0, high=5)
    assert all(r["components"][0]["outside"] for r in run_search(engine, with_limit)["rows"])
    positive = query(70, 30)
    positive["criteria"].append({"stat": "z", "weight": 1, "low": 0, "high": 10})
    assert run_search(engine, positive)["outcome"] == "missing_data"
    zero["thresholds"] = [{"stat": "z", "max": 10}]
    assert run_search(engine, zero)["outcome"] == "missing_data"
    bad_scale, duplicate, no_scale = query(70, 30), query(70, 30), query(70, 30)
    bad_scale["criteria"][0].update(low=2, high=1)
    duplicate["criteria"][1]["stat"] = "x"
    no_scale["criteria"][0]["stat"] = "z"
    for bad in [bad_scale, duplicate, no_scale, query(-1, 2), query(0, 0), query(1, 0),
                query("NaN", 1), query(float("inf"), 1), query(70, 30, 21)]:
        try:
            run_search(engine, bad)
        except ValueError:
            continue
        raise AssertionError(f"Принят ошибочный запрос {bad}")
    return "Все 15 баллов и префиксы K=2 совпали. Порядки ADEBC, BDEAC, ABDEC. Вклады A равны 70 и 7,5. Проверены единицы c/мс, веса 7/3, нулевой/положительный вес пропуска, шкала, порог и 9 ошибок ввода."


def _check_search_r3():
    engine = Engine(_rank_fixture())
    q = {"weapon_id": "demo", "criteria": [{"stat": "index"}]}
    locked = run_search(engine, dict(q, locks={"q": "empty"}))
    assert [r["value"] for r in locked["rows"]] == [12, 12, 10, 9]
    for action in ("stock", {"action": "explicit", "module_id": "p0"}):
        only = run_search(engine, dict(q, locks={"p": action}))
        assert len(only["rows"]) == 1 and only["rows"][0]["effective"]["p"] == "p0"
        assert only["rows"][0]["actions"]["p"] == action
    lo = run_search(engine, dict(q, thresholds=[{"stat": "index", "min": 12}]))
    assert [r["value"] for r in lo["rows"]] == [14, 12, 12]
    none = run_search(engine, dict(q, thresholds=[{"stat": "index", "min": 14, "min_open": True}]))
    assert none["complete"] and not none["rows"] and none["outcome"] == "no_candidates"
    missing = run_search(engine, dict(q, thresholds=[{"stat": "unreported", "max": 12}]))
    assert missing["complete"] and missing["outcome"] == "missing_data"
    conflict = run_search(engine, dict(q, locks={"p": "stock", "q": "q5"}))
    assert conflict["excluded"]["conflict"] == 1 and conflict["outcome"] == "no_candidates"
    assert conflict["reasons"]
    try:
        run_search(engine, dict(q, thresholds=[{"stat": "index", "min": 15}, {"stat": "index", "max": 14}]))
    except ValueError as exc:
        assert "thresholds" in str(exc)
    else:
        raise AssertionError("Приняты противоречивые границы")
    cancel = Event()
    cancel.set()
    stopped = run_search(engine, q, cancel)
    assert not stopped["complete"] and not stopped["rows"] and stopped["evaluated"] == 0
    assert stopped["outcome"] == "cancelled"
    scenario = _search_fixture({"capacity": (10, "max", "rounds")},
                               {"m": (False, None, {"short": {"capacity": -8}})})
    scenario["stats"]["ammo"] = {"name": "Запас", "kind": "derived", "meaning": "Тест",
            "direction": "max", "unit": "rounds", "precision": 0, "formula_id": "ammo",
            "dependencies": ["capacity"]}
    sc_query = {"weapon_id": "demo", "criteria": [{"stat": "capacity"}], "context": {"current_load": 5}}
    sc_result = run_search(Engine(scenario), sc_query)
    assert sc_result["excluded"]["scenario"] == 1 and [r["value"] for r in sc_result["rows"]] == [10]
    try:
        run_search(Engine(scenario), dict(sc_query, context={"health": 0}))
    except ValueError as exc:
        assert "Здоровье" in str(exc)
    else:
        raise AssertionError("Принято неверное здоровье цели")
    return "Пустой q исключил 14, фиксация p0 сохранила единственный состав и действие. Порог ≥12 дал 14,12,12; >14 — завершённый пустой список. Различены конфликт, пропуск данных, отмена и противоречивые границы."


def _check_search_r5():
    from queue import Queue
    from threading import Thread
    engine = Engine(_rank_fixture())
    requests = SearchRequests()
    inbox = Queue()
    old_ready, release = Event(), Event()
    a, stop_a = requests.new()
    first = {"weapon_id": "demo", "criteria": [{"stat": "index", "direction": "min"}]}
    second = {"weapon_id": "demo", "criteria": [{"stat": "index", "direction": "max"}]}

    def old_worker():
        result = run_search(engine, first, stop_a)
        old_ready.set()
        release.wait(3)
        inbox.put((a, result))

    worker = Thread(target=old_worker, daemon=True)
    worker.start()
    try:
        assert old_ready.wait(3)
        b, stop_b = requests.new()
        inbox.put((b, run_search(engine, second, stop_b)))
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and stop_a.is_set()
    displayed = None
    history = []
    while not inbox.empty():
        token, message = inbox.get_nowait()
        history.append(token)
        if requests.current(token):
            displayed = message
    assert history == [b, a] and displayed["query"]["criteria"][0]["direction"] == "max"
    assert displayed["rows"][0]["value"] == 14
    return "Два запроса через Engine и поток/очередь: пришли B, затем A. Старый токен отклонён, сохранена выдача B с первым значением 14."


def _check_search_bound():
    # 11⁴ различных составов, общие ресурсы и варианты со спорными эффектами.
    # Независимый эталон вычисляется для каждого состава без применения границы.
    slots = {}
    for i in range(4):
        mids = {f"m{i}_{j:02}": {"x": (6 if i == 0 and j == 1 else 0)} for j in range(11)}
        slots[f"s{i}"] = (True, f"m{i}_00", mids)
    cat = _search_fixture({"x": (10, "max", "pt"), "y": (2, "min", "pt")}, slots)
    cat["compatibility"]["demo_m0_01"]["excludes"] = ["m1_00"]
    cat["compatibility"]["demo_m0_01"]["provides"] = [{"type": "port", "point_id": "a", "capacity": 1}]
    cat["compatibility"]["demo_m1_01"]["requirements"] = [[{"type": "port", "provider_slot": "s0"}]]
    cat["compatibility"]["demo_m2_01"]["requirements"] = [[{"type": "port", "provider_slot": "s0"}]]
    cat["modules"]["m2_02"]["unresolved_effects"] = [
        {"state": "disputed", "value": None, "affected_stats": ["x"], "source_id": "s",
         "reason": "Синтетический пропуск", "raw_label": "?"}]
    engine = Engine(cat)
    candidates = []
    for a, b, c, d in product(range(11), repeat=4):
        if (a == 1 and b == 0) or c == 2 or int(b == 1) + int(c == 1) > int(a == 1):
            continue
        values = (a, b, c, d)
        key = tuple(f"m{i}_{j:02}" for i, j in enumerate(values))
        candidates.append((16 if a == 1 else 10, sum(bool(j) for j in values), key))
    for direction in ("max", "min"):
        q = {"weapon_id": "demo", "mode": "weighted", "k": 20, "criteria": [
            {"stat": "x", "direction": direction, "weight": 50, "low": 10, "high": 16},
            {"stat": "y", "direction": "min", "weight": 50, "low": 2, "high": 4}]}
        data = run_search(engine, q)
        expected = []
        for x, changes, key in candidates:
            contribution = 50 * Fraction((x - 10) if direction == "max" else (16 - x), 6)
            expected.append((-(50 + contribution), changes, key))
        expected.sort()
        got = [(-r["value"], r["changes"], tuple(r["effective"][s] for s in slots))
               for r in data["rows"]]
        assert data["complete"] and data["proof"] == "objective_bound" and data["pruned"] > 0
        assert got == expected[:20]
    return "Топ-20 обеих направленностей с весами и отсечением совпал с независимым перебором 14 641 состава при конкуренции точек, запрете сочетания и спорном эффекте."


def tests_search():
    return {"Р1": _check_search_r1() + " " + _check_search_bound(), "Р2": _check_search_r2(),
            "Р3": _check_search_r3(), "Р5": _check_search_r5()}


def benchmark_search():
    slots = {}
    for i in range(4):
        variants = {f"m{i}_{j}": {"x": j, "y": (j * (i + 1)) % 10} for j in range(10)}
        slots[f"slot{i}"] = (True, f"m{i}_0", variants)
    cat = _search_fixture({"x": (0, "max", "pt"), "y": (0, "min", "pt")}, slots)
    engine = Engine(cat)
    q = {"weapon_id": "demo", "mode": "weighted", "k": 20, "criteria": [
        {"stat": "x", "low": 0, "high": 36, "weight": 70},
        {"stat": "y", "low": 0, "high": 36, "weight": 30}]}
    updates = []
    def record(progress):
        updates.append(progress["elapsed"])
    result = run_search(engine, q, progress=record)
    reference = []
    for vals in product(range(10), repeat=4):
        x = sum(vals)
        y = sum((j * (i + 1)) % 10 for i, j in enumerate(vals))
        score = Fraction(70 * x + 30 * (36 - y), 36)
        key = tuple(f"m{i}_{j}" for i, j in enumerate(vals))
        reference.append((-score, sum(j != 0 for j in vals), key))
    reference.sort()
    got = [(-r["value"], r["changes"], tuple(r["effective"][s] for s in slots))
           for r in result["rows"]]
    assert result["evaluated"] == 10000 and result["complete"] and got == reference[:20]
    updates.append(result["elapsed"])
    return {"evaluated": result["evaluated"], "seconds": round(result["elapsed"], 4),
            "max_progress_interval_s": round(max(b-a for a, b in zip(updates, updates[1:])), 4),
            "first": str(result["rows"][0]["value"]), "last": str(result["rows"][-1]["value"])}


from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from decimal import Decimal
from datetime import datetime, timezone
import json


_COMPARE_STATES = {"unknown": "Неизвестно", "not_applicable": "Неприменимо",
                   "unreachable": "Недостижимо"}
_COMPARE_CONTEXT = {
    "scenario_shot_damage": ("hits",),
    "scenario_magazine_damage": ("hits", "magazine_model"),
    "shots_needed": ("hits", "health"),
    "ttk": ("hits", "health", "reserves", "current_load", "start_time",
            "magazine_transition", "cycle_transitions", "magazine_model"),
    "magazine_time": ("cycle_transitions", "magazine_model"),
    "magazine_damage": ("magazine_model",), "ammo": ("reserves", "magazine_model"),
}
_COMPARE_DEFAULTS = {"hits": "all", "health": 100, "reserves": 0,
                     "current_load": "full", "magazine_model": True}


def _snapshot_data(value):
    """Copy values into JSON types without rounding the calculation."""
    if isinstance(value, (Fraction, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: _snapshot_data(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_data(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_snapshot_data(v) for v in sorted(value)]
    return deepcopy(value)


def format_exact(value, precision=2):
    """Half-to-even using integer arithmetic, including long fractions."""
    x = Fraction(str(value))
    mult = 10 ** precision
    rounded = round(x * mult)
    digits = str(abs(rounded)).rjust(precision + 1, "0")
    number = digits if not precision else digits[:-precision] + "," + digits[-precision:]
    return ("−" if rounded < 0 else "") + number


def make_snapshot(engine, wid, actions, pins, context, title, query=None, result=None):
    cat = engine.catalog
    weapon = cat["weapons"][wid]
    evaluation = engine.evaluate(wid, actions, context)
    digest = getattr(engine, "_snapshot_digest", None)
    if digest is None:
        digest = engine._snapshot_digest = fingerprint(cat)
    locked = (dict(pins) if isinstance(pins, dict)
              else {sid: actions.get(sid, "stock") for sid in pins})
    metrics = {}
    for mid, measurement in evaluation["metrics"].items():
        base_id = mid.removesuffix("__rel")
        spec = cat["stats"].get(base_id, {})
        relative = base_id != mid
        metrics[mid] = {**measurement,
                        "name": spec.get("name", base_id) + (" · к штатной" if relative else ""),
                        "meaning": spec.get("meaning", base_id),
                        "unit": "%" if relative else spec.get("unit", ""),
                        "direction": spec.get("direction", "display_only"),
                        "precision": spec.get("precision", 2),
                        "formula": spec.get("formula_id", base_id)}
    scenario = dict(context)
    for ctx_key, stat in (("reserves", "reserves"), ("hits", "projectiles")):
        if ctx_key not in scenario:
            scenario[ctx_key] = evaluation["metrics"].get(stat, {}).get("value")
    scenario.setdefault("magazine_model", weapon.get("magazine_model", True))
    names = {"weapon": weapon["name"],
             "slots": {sid: slot.get("name", cat["slots"][slot["type"]]["name"])
                       for sid, slot in weapon["slots"].items()},
             "modules": {m: cat["modules"].get(m, {}).get("name", m)
                         for m in evaluation["effective"].values() if m}}
    return _snapshot_data({
        "preset_schema": 1, "title": title, "weapon_id": wid,
        "actions": {key: actions.get(key, "stock") for key in weapon["slots"]} | actions,
        "pins": locked, "locks": locked, "context": context, "scenario": scenario, "names": names,
        "digest": digest, "catalog_hash": digest, "game_version": cat["game_version"],
        "revision": cat["revision"], "calc_version": cat["calc_version"],
        "profile": (query or {}).get("profile", "published"),
        "effective": evaluation["effective"], "status": evaluation["status"],
        "diagnostics": evaluation["reasons"], "metrics": metrics,
        "query": query, "result": result, "created_at": datetime.now(timezone.utc).isoformat(),
    })


def _comparison_signature(snap, mid, metric):
    ctx = snap.get("scenario", snap.get("context", {}))
    conditions = {
        key: ctx.get(key, _COMPARE_DEFAULTS.get(key))
        for key in _COMPARE_CONTEXT.get(metric.get("formula", mid), ())
    }
    if mid == "_score":
        query = snap.get("query") or {}
        conditions.update(weapon=snap["weapon_id"], context=ctx,
                          criteria=query.get("criteria"), mode=query.get("mode"))
    for key, value in conditions.items():
        if isinstance(value, (int, float, str, Decimal, Fraction)) and not isinstance(value, bool):
            try:
                conditions[key] = str(Fraction(str(value)))
            except (ValueError, ZeroDivisionError):
                pass
    return (snap.get("digest"), snap.get("calc_version"), snap.get("profile"),
            metric.get("unit"), metric.get("meaning"), metric.get("formula"),
            snap.get("weapon_id") if mid.endswith("__rel") else None,
            json.dumps(conditions, ensure_ascii=False, sort_keys=True))


def _compare_metric(snaps, metrics, mid, ref, differences):
    records = [
        vals.get(mid, {"state": "unknown" if snap.get("status") != "compatible" else "not_applicable"})
        for vals, snap in zip(metrics, snaps)
    ]
    info = next((m for m in records if "name" in m), {})
    precision = info.get("precision", 2)
    signatures = [_comparison_signature(s, mid, m) for s, m in zip(snaps, records)]
    values = [Fraction(str(m["value"])) if m.get("state") == "known" else None for m in records]
    if differences and len(set(zip(signatures, (m["state"] for m in records), values))) == 1:
        return None
    groups = defaultdict(list)
    for i, v in enumerate(values):
        if v is not None:
            groups[signatures[i]].append(i)
    leaders = []
    for indices in groups.values():
        direction = records[indices[0]].get("direction")
        if len(indices) >= 2 and direction in ("min", "max"):
            best = (min if direction == "min" else max)(values[i] for i in indices)
            leaders.extend(i for i in indices if values[i] == best)
    cells = []
    for i, (value, record) in enumerate(zip(values, records)):
        if value is None:
            cells.append(_COMPARE_STATES.get(record.get("state"), "Неизвестно"))
            continue
        unit = record.get("unit", "")
        cell = f"{format_exact(value, precision)} {unit}".rstrip()
        if i != ref and values[ref] is not None:
            if signatures[i] != signatures[ref]:
                cell += " · справочно"
            else:
                delta = value - values[ref]
                delta_unit = "п.п." if mid.endswith("__rel") else unit
                prefix = "+" if delta > 0 else ""
                cell += f" · Δ {prefix}{format_exact(delta, precision)} {delta_unit}".rstrip()
                if values[ref] != 0 and not mid.endswith("__rel") and mid != "_score":
                    pct = 100 * delta / values[ref]
                    cell += " (" + ("+" if pct > 0 else "") + format_exact(pct, 2) + "%)"
        if i in leaders:
            cell += " · лидер"
        cells.append(cell)
    return {"id": mid, "name": info.get("name", mid), "cells": cells, "leaders": sorted(leaders)}


def compare_snapshots(snapshots, reference=0, differences=False):
    if not snapshots:
        return []
    if len(snapshots) > 4 or not 0 <= reference < len(snapshots):
        raise ValueError("Сравнение допускает до четырёх снимков. Выберите опору из таблицы.")
    metrics = [dict(snap.get("metrics", {})) for snap in snapshots]
    for snap, vals in zip(snapshots, metrics):
        result, query = snap.get("result") or {}, snap.get("query") or {}
        if query.get("mode") == "weighted" and result.get("value") is not None:
            vals["_score"] = {"state": "known", "value": result["value"], "name": "Итоговый балл",
                              "meaning": "Сумма вкладов", "unit": "балл", "direction": "max",
                              "precision": 2, "formula": "_score"}
    rows = []
    for mid in dict.fromkeys(key for vals in metrics for key in vals):
        row = _compare_metric(snapshots, metrics, mid, reference, differences)
        if row:
            rows.append(row)
    slots = dict.fromkeys(sid for snap in snapshots for sid in (snap.get("effective", {}) | snap.get("lost_slots", {})))
    for sid in slots:
        keys, labels = [], []
        name = sid
        for snap in snapshots:
            name = snap.get("names", {}).get("slots", {}).get(sid, name)
            if sid in snap.get("lost_slots", {}):
                old = snap["lost_slots"][sid]
                title = snap.get("names", {}).get("modules", {}).get(old, old)
                keys.append(("lost", old))
                labels.append(f"Утрачено · {title or sid} [{old or sid}]")
                continue
            if sid not in snap.get("effective", {}):
                keys.append(("absent", None))
                labels.append("Неприменимо")
                continue
            names = snap.get("names", {})
            name = names.get("slots", {}).get(sid, sid)
            mid = snap["effective"][sid]
            title = names.get("modules", {}).get(mid)
            keys.append(("part", mid) if title or mid is None else ("lost", mid))
            labels.append("Пусто" if mid is None else (title or f"Утрачено · {mid}"))
        if not differences or len(set(keys)) > 1:
            rows.append({"id": f"slot.{sid}", "name": "Слот · " + name, "cells": labels, "leaders": []})
    return rows


# Desktop application. Tk is imported only when the graphical entry point is used.
import copy
import json
import queue
import threading
from fractions import Fraction


_GUI_STATES = {"compatible": "Совместимо", "unknown": "Требует проверки", "conflict": "Несовместимо"}
_GUI_COLORS = {"compatible": "#14644e", "unknown": "#825300", "conflict": "#ae363f"}
_METRIC_STATES = {"unknown": "неизвестно", "unreachable": "недостижимо", "not_applicable": "неприменимо"}
_OUTCOMES = {"results": "Есть результаты", "no_candidates": "Нет допустимых составов",
             "missing_data": "Недостаточно данных", "cancelled": "Поиск отменён",
             "running": "Идёт поиск"}


def _ui_number(value, digits=2):
    if value is None:
        return "—"
    number = Fraction(str(value))
    fixed = round(number * 10 ** digits)
    absolute = abs(fixed)
    if digits:
        whole, remainder = divmod(absolute, 10 ** digits)
        text = f"{whole},{remainder:0{digits}d}".rstrip("0").rstrip(",")
    else:
        text = str(absolute)
    return ("−" if fixed < 0 else "") + text


def _ui_action(value):
    if isinstance(value, dict):
        return value.get("module_id") if value.get("action") == "explicit" else value.get("action", "stock")
    return value if value is not None else "empty"


def _ui_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class BodycamApp:
    def __init__(self, root, catalog_path, db_path=None):
        global tk, ttk, messagebox, simpledialog
        import tkinter as tk
        from tkinter import ttk, messagebox, simpledialog
        self.root, self.catalog_path = root, catalog_path
        self.store = Store(db_path)
        self.catalog = self.engine = self.active_catalog = None
        self.hash = self.active_hash = None
        self.wid = self.preset = self.origin = None
        self.actions, self.locks, self.context = {}, {}, {}
        self.dirty = False
        self.snapshots, self.criteria, self.thresholds = [], [], []
        self.requests, self.events = SearchRequests(), queue.Queue()
        self.token = self.cancel = self.report = None
        self.slot = self.evaluation = None
        self.latest_query = None
        self._pending_event = None
        self._closing = False
        root.title("Bodycam · Конфигуратор сборок")
        root.geometry("1180x780")
        root.minsize(890, 620)
        from tkinter import font as tkfont
        families = {family.casefold(): family for family in tkfont.families(root)}
        self.ui_family = next((families[f.casefold()] for f in ("Segoe UI", "DejaVu Sans", "Arial", "Noto Sans")
                               if f.casefold() in families), tkfont.nametofont("TkDefaultFont").actual("family"))
        for font_name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont", "TkCaptionFont"):
            tkfont.nametofont(font_name).configure(family=self.ui_family)
        root.option_add("*Font", "TkDefaultFont")
        self.style = ttk.Style(root)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self.style.configure("TNotebook.Tab", padding=(14, 7))
        self.style.configure("Treeview", rowheight=25)
        self.style.configure("Heading.TLabel", font=(self.ui_family, 11, "bold"))
        self.style.configure("Hero.TLabel", font=(self.ui_family, 16, "bold"),
                             foreground="white", background="#203044")
        self.style.configure("HeroSub.TLabel", foreground="#d9e4eb", background="#203044")
        self.style.configure("Hero.TFrame", background="#203044")
        self.style.configure("Main.TButton", padding=(10, 5))
        self.notice = tk.StringVar(master=root)
        self.revision = tk.StringVar(master=root, value="Справочник не открыт")
        self.model = tk.StringVar(master=root)
        self.profile = "published"
        top = ttk.Frame(root, padding=(14, 9), style="Hero.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Bodycam", style="Hero.TLabel").pack(side="left")
        ttk.Label(top, text="Конфигуратор сборок", style="HeroSub.TLabel",
                  padding=(10, 0)).pack(side="left")
        ttk.Button(top, text="Перечитать справочник", command=self.read_catalog).pack(side="right")
        ttk.Label(top, textvariable=self.revision, style="HeroSub.TLabel",
                  padding=(10, 0)).pack(side="right")
        bar = ttk.Frame(root, padding=(14, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Модель").pack(side="left", padx=(0, 6))
        self.model_box = ttk.Combobox(bar, width=36, textvariable=self.model, state="readonly")
        self.model_box.pack(side="left")
        self.model_box.bind("<<ComboboxSelected>>", self._model_selected)
        ttk.Button(bar, text="Условия расчёта…", command=self.context_dialog).pack(side="left", padx=6)
        self.summary = ttk.Label(bar)
        self.summary.pack(side="left", padx=6)
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10)
        self.pages = {}
        for title in ("Конструктор", "Подбор", "Сравнение", "Пресеты"):
            page = ttk.Frame(self.nb, padding=8)
            self.nb.add(page, text=title)
            self.pages[title] = page
        self._build_editor()
        self._build_search()
        self._build_compare()
        self._build_presets()
        ttk.Label(root, textvariable=self.notice, anchor="w", wraplength=950,
                  padding=(14, 5)).pack(fill="x")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-s>", lambda event: self.save_editor())
        self.read_catalog(startup=True)
        self.refresh_presets()
        self._pending_event = root.after(120, self.poll_search)

    @staticmethod
    def _tree(parent, cols, height=8, selectmode="browse", horizontal=False):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[key for key, _, _ in cols], show="headings",
                            height=height, selectmode=selectmode)
        for key, title, width in cols:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=min(50, width), anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        if horizontal:
            hb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
            tree.configure(xscrollcommand=hb.set)
            hb.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.pack(fill="both", expand=True)
        for tag, color in _GUI_COLORS.items():
            tree.tag_configure(tag, foreground=color)
        return tree

    @staticmethod
    def _wipe(tree):
        items = tree.get_children()
        if items:
            tree.delete(*items)

    @staticmethod
    def _button(bar, label, command):
        return ttk.Button(bar, text=label, command=command).pack(side="left", padx=(0, 4), pady=2)

    @staticmethod
    def _heading(parent, text):
        ttk.Label(parent, text=text, style="Heading.TLabel").pack(anchor="w", pady=(1, 4))

    @staticmethod
    def _text_box(parent, height=5):
        area = ttk.Frame(parent)
        area.pack(fill="both", expand=True)
        text = tk.Text(area, wrap="word", height=height, bd=1, padx=7, pady=5,
                       background="#fafbfc", font="TkDefaultFont", relief="solid")
        scroll = ttk.Scrollbar(area, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    @staticmethod
    def _write(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _build_editor(self):
        page = self.pages["Конструктор"]
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        for label, cmd in [("Сохранить…", self.save_editor), ("В сравнение", self.compare_editor),
                           ("Сбросить к штатной", self.reset_stock)]:
            self._button(bar, label, cmd)
        self.state_label = ttk.Label(bar, font=(self.ui_family, 10, "bold"))
        self.state_label.pack(side="right")
        self.diag = ttk.Label(page, wraplength=970)
        self.diag.pack(fill="x", pady=(2, 6))
        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(expand=True, fill="both")
        left = ttk.Frame(panes, padding=(0, 0, 5))
        mid = ttk.Frame(panes, padding=(5, 0))
        right = ttk.Frame(panes, padding=(5, 0, 0))
        for panel, weight in [(left, 3), (mid, 3), (right, 4)]:
            panes.add(panel, weight=weight)
        self._heading(left, "Слоты")
        self.slot_tree = self._tree(left, [("slot", "Слот", 108), ("part", "Выбор", 135), ("lock", "Фикс.", 43)])
        self.slot_tree.bind("<<TreeviewSelect>>", self._slot_changed)
        self.lock_button = ttk.Button(left, text="Закрепить / открепить слот", command=self.toggle_lock)
        self.lock_button.pack(fill="x", pady=4)
        self._heading(mid, "Детали выбранного слота")
        self.part_filter, self.all_parts = tk.StringVar(), tk.BooleanVar()
        entry = ttk.Entry(mid, textvariable=self.part_filter)
        entry.pack(fill="x")
        self.part_filter.trace_add("write", lambda *_: self.render_choices())
        ttk.Checkbutton(mid, text="Показать также спорные и конфликтные",
                        variable=self.all_parts, command=self.render_choices).pack(anchor="w")
        self.choice_tree = self._tree(mid, [("module", "Выбор", 136), ("status", "Установка", 94)])
        self.choice_tree.bind("<Double-1>", lambda e: self.install_selected())
        self.choice_tree.bind("<Return>", lambda e: self.install_selected())
        cb = ttk.Frame(mid)
        cb.pack(fill="x", pady=2)
        self._button(cb, "Применить", self.install_selected)
        self._button(cb, "Карточка…", self.part_card)
        self.why_part = ttk.Label(mid, wraplength=275)
        self.why_part.pack(fill="x")
        self.choice_tree.bind("<<TreeviewSelect>>", lambda e: self._choice_reason())
        self._heading(right, "Показатели")
        self.metric_tree = self._tree(right, [("metric", "Показатель", 175), ("value", "Значение", 96)])
        self.metric_tree.bind("<<TreeviewSelect>>", lambda e: self._metric_reason())
        self.metric_note = ttk.Label(right, wraplength=290)
        self.metric_note.pack(fill="x")

    def _model_selected(self, event=None):
        index = self.model_box.current()
        if index >= 0 and self.catalog:
            wid = self.model_ids[index]
            if not self.select_weapon(wid):
                self.model.set(self.model_labels.get(self.wid, ""))

    def select_weapon(self, wid):
        if self.wid == wid:
            return True
        if not self.catalog or wid not in self.catalog["weapons"] or not self.may_leave():
            return False
        self._load_editor(wid)
        return True

    def _use_catalog(self, cat, digest):
        self.catalog, self.hash, self.engine = cat, digest, Engine(cat)
        self.engine._snapshot_digest = digest
        self.model_ids = sorted(cat["weapons"], key=lambda w: (cat["weapons"][w]["class"],
                                                                 cat["weapons"][w]["name"], w))
        self.model_labels = {w: f'{cat["weapons"][w]["class"]}  /  {cat["weapons"][w]["name"]}'
                             for w in self.model_ids}
        self.model_box.configure(values=[self.model_labels[w] for w in self.model_ids])
        self.revision.set(f'Игра {cat["game_version"]}   ·   Ревизия {cat["revision"]}   ·   Расчёт {cat["calc_version"]}')
        self.criteria, self.thresholds = [], []
        self.metric_options = {}
        self.threshold_options = {}
        for sid, s in cat["stats"].items():
            for metric, unit in [(sid, s["unit"]), (sid + "__rel", "% к штатной")]:
                label = f'{s["name"]} ({unit}) · {metric}'
                self.threshold_options[label] = metric
                if s["direction"] in ("min", "max"):
                    self.metric_options[label] = metric
        self._criterion_defaults()

    def read_catalog(self, startup=False):
        if not startup and not self.may_leave():
            return
        previous_wid = self.wid
        try:
            cat = load_catalog(self.catalog_path)
            digest = self.store.activate(cat)
            status = "Справочник открыт."
        except Exception as exc:
            if self.active_catalog is not None:
                self.notice.set(f"Справочник отклонён. {exc}")
                messagebox.showerror("Перечитать справочник", str(exc), parent=self.root)
                return
            try:
                cat = self.store.current()
            except Exception as db_exc:
                self.notice.set(f"Не удалось прочитать каталог и архив. {exc}. {db_exc}")
                return
            if cat is None:
                self.notice.set(f"Исправьте справочник и нажмите «Перечитать справочник». {exc}")
                return
            digest = fingerprint(cat)
            status = f"Открыта последняя рабочая ревизия из архива. {exc}"
        self.active_catalog, self.active_hash = cat, digest
        self._use_catalog(cat, digest)
        target = previous_wid if previous_wid in cat["weapons"] else next(iter(self.model_ids), None)
        self._load_editor(target)
        self.notice.set(" ".join(s for s in (status, self.store.notice) if s))

    def _load_editor(self, wid, actions=None, locks=None, context=None, preset=None, origin=None):
        self.invalidate_search()
        self.wid = wid
        self.actions = copy.deepcopy(actions or {})
        self.locks = {sid: self.actions.get(sid, "stock") for sid in (locks or {})}
        self.context = copy.deepcopy(context or {})
        self.preset, self.origin = copy.deepcopy(preset), copy.deepcopy(origin)
        self.dirty, self.slot = False, None
        self.model.set(self.model_labels.get(wid, str(wid or "")))
        self.refresh_editor()

    def may_leave(self):
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("Несохранённая сборка",
                    "Сохранить изменения?\nДа — сохранить, Нет — отбросить, Отмена — остаться.",
                    parent=self.root)
        return self.save_editor() if answer else answer is False

    def _mark_changed(self):
        self.dirty, self.origin = True, None
        self.invalidate_search()
        self.refresh_editor()

    def _module_name(self, mid):
        if not mid:
            return "Пусто"
        return self.catalog["modules"].get(mid, {}).get("name") or (
            (self.preset or {}).get("names", {}).get("modules", {}).get(mid, mid) + " (утрачено)")

    def _slot_label(self, sid):
        w = self.catalog["weapons"].get(self.wid, {})
        spec = w.get("slots", {}).get(sid, {})
        label = spec.get("name") or self.catalog["slots"].get(spec.get("type"), {}).get("name", sid)
        if sum(s.get("type") == spec.get("type") for s in w.get("slots", {}).values()) > 1:
            label += f" ({sid})"
        return label

    def refresh_editor(self):
        self._wipe(self.slot_tree)
        self._wipe(self.metric_tree)
        self.metric_note.config(text="")
        self.summary.config(text="Условная цель, постоянный урон" + ("   •   есть изменения" if self.dirty else ""))
        if not self.catalog or self.wid not in self.catalog["weapons"]:
            self.evaluation = None
            self.state_label.configure(text="Модель недоступна", foreground=_GUI_COLORS["unknown"])
            self.diag.configure(text="Сведения о сохранённом составе доступны на экране пресетов.")
            self.render_choices()
            return
        w = self.catalog["weapons"][self.wid]
        self.context_error = ""
        try:
            e = self.engine.evaluate(self.wid, self.actions, self.context)
        except ValueError as err:
            self.context_error = str(err)
            e = dict(self.engine.compatibility(self.wid, self.actions), metrics={})
        self.evaluation = e
        state = e["status"]
        self.state_label.configure(text=_GUI_STATES[state], foreground=_GUI_COLORS[state])
        self.diag.configure(text=("Исправьте условия расчёта. " + self.context_error) if self.context_error
                            else " • ".join(e.get("reasons", [])))
        slots = sorted(w["slots"], key=lambda s: (w["slots"][s]["order"], s))
        extra = [sid for sid in self.actions if sid not in w["slots"]]
        for sid in slots + extra:
            act = _ui_action(self.actions.get(sid, "stock"))
            name = self._module_name(e.get("effective", {}).get(sid))
            if sid in extra:
                name = f"Утрачен слот · {act}"
            elif act == "stock":
                name = f"Штатно · {name}"
            elif act not in ("empty", "stock"):
                name = self._module_name(act)
            tag = (state,) if sid in e.get("slot_reasons", {}) or sid in extra else ()
            self.slot_tree.insert("", "end", iid=sid, values=(self._slot_label(sid), name,
                                    "да" if sid in self.locks else ""), tags=tag)
        self.slot = self.slot if self.slot in slots + extra else next(iter(slots), None)
        if self.slot is not None:
            self.slot_tree.selection_set(self.slot)
            self.slot_tree.focus(self.slot)
        if state == "compatible":
            for mid, measure in e.get("metrics", {}).items():
                spec = self.catalog["stats"].get(mid.removesuffix("__rel"), {})
                relative = mid.endswith("__rel")
                title = spec.get("name", mid) + (" · к штатной" if relative else "")
                if measure["state"] == "known":
                    unit = "%" if relative else spec.get("unit", "")
                    display = f'{_ui_number(measure["value"], spec.get("precision", 2))} {unit}'
                else:
                    display = _METRIC_STATES.get(measure["state"], measure["state"])
                self.metric_tree.insert("", "end", iid=mid, values=(title, display))
        else:
            self.metric_note.configure(text="Числовые итоги недоступны для этого черновика.")
        self.render_choices()

    def _metric_reason(self):
        item = self.metric_tree.selection()
        if item and self.evaluation:
            m = self.evaluation["metrics"].get(item[0], {})
            info = m.get("reason", "") or self.catalog["stats"].get(
                item[0].removesuffix("__rel"), {}).get("meaning", "")
            if "mul" in m and "add" in m:
                info += (f'   Множитель {_ui_number(m["mul"], 6)}; '
                         f'прибавка {_ui_number(m["add"], 6)}.')
            self.metric_note.configure(text=info)

    def _slot_changed(self, event):
        selected = self.slot_tree.selection()
        if selected and self.slot != selected[0]:
            self.slot = selected[0]
            self.render_choices()

    def render_choices(self):
        self._wipe(self.choice_tree)
        self.choice_actions, self.choice_notes = {}, {}
        self.why_part.configure(text="")
        if not self.catalog or self.wid not in self.catalog["weapons"]:
            return
        slot = self.catalog["weapons"][self.wid]["slots"].get(self.slot)
        if not slot:
            return
        current = _ui_action(self.actions.get(self.slot, "stock"))
        mods = self.catalog["modules"]
        ids = sorted((key for key, m in mods.items() if m["slot_type"] == slot["type"]),
                     key=lambda key: (mods[key]["name"].casefold(), key))
        options = [("stock", "Штатно · " + self._module_name(slot["stock_module_id"])), ("empty", "Снять")]
        options.extend((key, mods[key]["name"]) for key in ids)
        if current not in {key for key, _ in options}:
            options.append((current, self._module_name(current)))
        query = self.part_filter.get().casefold().strip()
        for idx, (act, label) in enumerate(options):
            if query and query not in label.casefold() and act != current:
                continue
            result = self.engine.compatibility(self.wid, {**self.actions, self.slot: act})
            state = result["status"]
            if state != "compatible" and not self.all_parts.get() and act != current:
                continue
            iid = str(idx)
            self.choice_actions[iid] = act
            self.choice_notes[iid] = " • ".join(result.get("reasons", []))
            self.choice_tree.insert("", "end", iid=iid, values=(label, _GUI_STATES[state]), tags=(state,))
            if act == current:
                self.choice_tree.selection_set(iid)
                self.why_part.configure(text=self.choice_notes[iid])

    def _choice_reason(self):
        selected = self.choice_tree.selection()
        if selected:
            self.why_part.configure(text=self.choice_notes.get(selected[0], ""))

    def install_selected(self):
        selected = self.choice_tree.selection()
        if selected and self.slot and selected[0] in self.choice_actions:
            self.set_action(self.slot, self.choice_actions[selected[0]])

    def set_action(self, slot, action):
        self.actions[slot] = copy.deepcopy(action)
        if slot in self.locks:
            self.locks[slot] = copy.deepcopy(action)
        self._mark_changed()

    def toggle_lock(self):
        if self.slot is None:
            return
        if self.slot in self.locks:
            del self.locks[self.slot]
        else:
            self.locks[self.slot] = self.actions.get(self.slot, "stock")
        self._mark_changed()

    def reset_stock(self):
        if self.wid and messagebox.askyesno("Штатная сборка", "Сбросить выбор деталей и закрепления?",
                                            parent=self.root):
            self.actions, self.locks = {}, {}
            self._mark_changed()

    def _viewer(self, title, content):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("680x500")
        self._write(self._text_box(win), content)
        ttk.Button(win, text="Закрыть", command=win.destroy).pack(pady=5)
        return win

    def part_card(self):
        selected = self.choice_tree.selection()
        if not selected or not self.catalog or self.slot is None:
            return
        mid = self.choice_actions.get(selected[0])
        if mid == "stock":
            mid = self.catalog["weapons"][self.wid]["slots"][self.slot]["stock_module_id"]
        if not mid or mid == "empty":
            self.notice.set("В выбранном варианте слот пуст.")
            return
        module = self.catalog["modules"].get(mid, {})
        records = [c for c in self.catalog["compatibility"].values() if c["module_id"] == mid]
        pair = next((c for c in records if c["weapon_id"] == self.wid), None)
        allowed = [self.catalog["weapons"][c["weapon_id"]]["name"] for c in records if c["status"] == "allowed"]
        source_ids = set()
        def collect_sources(value):
            if isinstance(value, dict):
                if value.get("source_id"):
                    source_ids.add(value["source_id"])
                for child in value.values():
                    collect_sources(child)
            elif isinstance(value, list):
                for child in value:
                    collect_sources(child)
        collect_sources(module)
        collect_sources(pair)
        source_records = {sid: self.catalog["sources"].get(sid, {}) for sid in sorted(source_ids)}
        content = [f'{self._module_name(mid)}   ·   {mid}', self.revision.get(),
                   f'Слот — {self._slot_label(self.slot)}', "Подтверждённые модели — " + ", ".join(allowed),
                   "Текущий состав — " + (self.choice_notes[selected[0]] or "Совместимо"),
                   "\nДопуск, требования, поставщики и точки", _ui_json(pair) if pair else "Нет сведений",
                   "\nСведения о детали и игровые эффекты", _ui_json(module),
                   "\nИсточники", _ui_json(source_records)]
        self._viewer("Карточка детали", "\n".join(content))

    def context_dialog(self):
        if not self.catalog or self.wid not in self.catalog["weapons"]:
            return
        fields = [("health", "Здоровье условной цели", "100"), ("hits", "Попаданий за выстрел", ""),
                  ("reserves", "Полных запасных магазинов", ""), ("current_load", "Текущий заряд", ""),
                  ("start_time", "Подготовка при пустом старте, с", ""),
                  ("magazine_transition", "Переход между магазинами, с", "")]
        win = tk.Toplevel(self.root)
        win.title("Условия расчёта")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=14)
        frame.pack()
        ttk.Label(frame, text="Условная цель, постоянный урон", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        variables, initial = {}, {}
        for i, (key, label, default) in enumerate(fields, 1):
            stored = self.context.get(key, default)
            initial[key] = "" if stored is None else str(stored)
            variables[key] = tk.StringVar(value=initial[key])
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(frame, width=26, textvariable=variables[key]).grid(row=i, column=1, sticky="ew")
        pattern = self.context.get("cycle_transitions") or []
        initial["cycle_transitions"] = " ".join("?" if t is None else str(t) for t in pattern)
        variables["cycle_transitions"] = tk.StringVar(value=initial["cycle_transitions"])
        ttk.Label(frame, text="Цикл переходов между выстрелами, с").grid(row=7, column=0, sticky="w")
        ttk.Entry(frame, width=26, textvariable=variables["cycle_transitions"]).grid(row=7, column=1, sticky="ew")
        ttk.Label(frame, text="Секунды через пробел, например 0,1 0,5. Знак ? означает неизвестный переход."
                  ).grid(row=8, column=0, columnspan=2, sticky="w", pady=3)
        feeding = {"По модели": None, "Полные магазины": True, "Без магазинной модели": False}
        initial["magazine_model"] = next(label for label, value in feeding.items()
                                         if value is self.context.get("magazine_model"))
        variables["magazine_model"] = tk.StringVar(value=initial["magazine_model"])
        ttk.Label(frame, text="Модель питания").grid(row=9, column=0, sticky="w", pady=2)
        ttk.Combobox(frame, state="readonly", values=list(feeding), textvariable=variables["magazine_model"]
                     ).grid(row=9, column=1, sticky="ew")
        ttk.Label(frame, text="Пустые поля используют условия модели. По умолчанию магазин полный,\n"
                              "попадают все снаряды.").grid(row=10, column=0, columnspan=2, sticky="w", pady=8)

        def commit():
            ctx = copy.deepcopy(self.context)
            try:
                for key, field in variables.items():
                    raw = field.get().strip()
                    if raw == initial[key]:
                        continue
                    if not raw:
                        ctx.pop(key, None)
                        continue
                    if key == "magazine_model":
                        if feeding[raw] is None:
                            ctx.pop(key, None)
                        else:
                            ctx[key] = feeding[raw]
                        continue
                    text = raw.replace(",", ".")
                    if key == "cycle_transitions":
                        terms = [None if token == "?" else token for token in text.split()]
                        if any(t is not None and Fraction(t) < 0 for t in terms):
                            raise ValueError("Длительность перехода должна быть неотрицательной.")
                        ctx[key] = terms
                        continue
                    value = Fraction(text)
                    if value < 0 or key == "health" and value == 0:
                        raise ValueError("Проверьте знак введённого значения.")
                    if key in ("hits", "reserves", "current_load"):
                        if value.denominator != 1:
                            raise ValueError("Попадания, запас и заряд задаются целыми числами.")
                        ctx[key] = int(value)
                    else:
                        ctx[key] = text
                self.engine.evaluate(self.wid, self.actions, ctx)
            except (ValueError, ZeroDivisionError) as exc:
                messagebox.showerror("Условия", str(exc), parent=win)
                return
            win.destroy()
            if self.context != ctx:
                self.context = ctx
                self._mark_changed()
        ttk.Button(frame, text="Применить", command=commit).grid(row=11, column=1, sticky="e")
        win.grab_set()

    def _build_search(self):
        page = self.pages["Подбор"]
        control = ttk.Frame(page)
        control.pack(fill="x")
        self.mode = tk.StringVar(value="Одна цель")
        ttk.Combobox(control, textvariable=self.mode, state="readonly", width=16,
                     values=["Одна цель", "По весам"]).pack(side="left")
        ttk.Label(control, text="   Размер топа ").pack(side="left")
        self.top_k = tk.StringVar(value="5")
        ttk.Spinbox(control, textvariable=self.top_k, from_=1, to=20, width=4).pack(side="left", padx=(0, 10))
        self._button(control, "Найти сборки", self.start_search)
        self._button(control, "Отменить поиск", self.cancel_search)
        self.search_status = tk.StringVar(value="Задайте цель поиска.")
        ttk.Label(page, textvariable=self.search_status, wraplength=1050).pack(anchor="w", pady=3)
        settings = ttk.Panedwindow(page, orient="horizontal")
        settings.pack(fill="x", pady=(0, 4))
        criteria = ttk.LabelFrame(settings, text="Цели и ориентиры", padding=3)
        thresholds = ttk.LabelFrame(settings, text="Обязательные пределы", padding=3)
        settings.add(criteria, weight=3)
        settings.add(thresholds, weight=2)
        self.criteria_tree = self._tree(criteria, [("name", "Показатель", 160), ("dir", "Цель", 62),
            ("weight", "Вес / доля", 85), ("scale", "Шкала", 85)], height=2)
        self.threshold_tree = self._tree(thresholds, [("name", "Показатель", 155),
                                             ("min", "От", 54), ("max", "До", 54)], height=2)
        for kind, panel, tree in [("criteria", criteria, self.criteria_tree),
                                   ("threshold", thresholds, self.threshold_tree)]:
            buttons = ttk.Frame(panel)
            buttons.pack(fill="x")
            self._button(buttons, "Добавить", lambda k=kind: self.rule_dialog(k))
            self._button(buttons, "Изменить", lambda k=kind: self.rule_dialog(k, True))
            self._button(buttons, "Убрать", lambda k=kind: self.delete_rule(k))
            tree.bind("<Double-1>", lambda e, k=kind: self.rule_dialog(k, True))
        split = ttk.Panedwindow(page, orient="vertical")
        split.pack(fill="both", expand=True)
        results, detail = ttk.Frame(split), ttk.Frame(split)
        split.add(results, weight=2)
        split.add(detail, weight=3)
        self.results = self._tree(results, [("rank", "Место", 50), ("value", "Цель / балл", 110),
                             ("changes", "Замен к штатной", 110), ("parts", "Отличия от редактора", 520)], height=4)
        self.results.bind("<<TreeviewSelect>>", lambda e: self.result_detail())
        rb = ttk.Frame(detail)
        rb.pack(fill="x")
        for label, cmd in [("В конструктор", self.edit_result), ("В сравнение", self.compare_result),
                           ("Сохранить результат…", self.save_result)]:
            self._button(rb, label, cmd)
        self.detail = self._text_box(detail, 5)

    def _criterion_defaults(self):
        if self.catalog and self.metric_options:
            ids = list(self.metric_options.values())
            sid = "capacity" if "capacity" in ids else ids[0]
            self.criteria = [{"stat": sid, "direction": self.catalog["stats"][sid]["direction"], "weight": "1"}]
        self.render_rules()

    def _metric_title(self, mid, display_unit=None):
        spec = self.catalog["stats"].get(mid.removesuffix("__rel"), {}) if self.catalog else {}
        name = spec.get("name", mid)
        unit = "% к штатной" if mid.endswith("__rel") else display_unit or spec.get("unit", "")
        return f"{name} ({unit})" if unit else name

    def render_rules(self):
        self._wipe(self.criteria_tree)
        self._wipe(self.threshold_tree)
        weights = [Fraction(str(c.get("weight", 1))) for c in self.criteria]
        total = sum(weights)
        for i, c in enumerate(self.criteria):
            sid = c["stat"]
            scale = self.catalog["stats"].get(sid, {}).get("scale", {}) if self.catalog else {}
            low, high = c.get("low", scale.get("low")), c.get("high", scale.get("high"))
            share = _ui_number(100 * weights[i] / total, 1) + "%" if total else "—"
            self.criteria_tree.insert("", "end", iid=str(i), values=(self._metric_title(sid, c.get("unit")),
                "макс." if c.get("direction") == "max" else "мин.",
                f"{c.get('weight', 1)} / {share}", f"{low} … {high}" if low is not None and high is not None else "не задана"))
        for i, t in enumerate(self.thresholds):
            self.threshold_tree.insert("", "end", iid=str(i), values=(
                self._metric_title(t["stat"], t.get("unit")), t.get("min", "—"), t.get("max", "—")))

    def delete_rule(self, kind):
        values, tree = ((self.criteria, self.criteria_tree) if kind == "criteria"
                        else (self.thresholds, self.threshold_tree))
        if tree.selection():
            del values[int(tree.selection()[0])]
            self.invalidate_search()
            self.render_rules()

    def rule_dialog(self, kind, edit=False):
        criterion = kind == "criteria"
        if not self.catalog or not (self.metric_options if criterion else self.threshold_options):
            return
        seq, tree = (self.criteria, self.criteria_tree) if criterion else (self.thresholds, self.threshold_tree)
        if edit and not tree.selection():
            self.notice.set("Выберите строку для изменения.")
            return
        idx = int(tree.selection()[0]) if edit else None
        rule = seq[idx] if edit else {}
        win = tk.Toplevel(self.root)
        win.transient(self.root)
        win.title("Цель подбора" if criterion else "Обязательный предел")
        fields = ttk.Frame(win, padding=12)
        fields.pack()
        options = self.metric_options if criterion else self.threshold_options
        labels = list(options)
        selected = next((s for s in labels if options[s] == rule.get("stat")), labels[0])
        metric = tk.StringVar(value=selected)
        ttk.Label(fields, text="Показатель и представление").grid(row=0, column=0, sticky="w", padx=3)
        combo = ttk.Combobox(fields, width=47, state="readonly", textvariable=metric, values=labels)
        combo.grid(row=0, column=1)
        variables = {}
        if criterion:
            params = [("direction", "Направление", "max"), ("weight", "Вес", "1"),
                      ("low", "Нижний ориентир", ""), ("high", "Верхний ориентир", "")]
        else:
            params = [("min", "Минимум включительно", ""), ("max", "Максимум включительно", "")]
        for i, (key, label, default) in enumerate(params, 1):
            var = variables[key] = tk.StringVar(value=str(rule.get(key, default)))
            ttk.Label(fields, text=label).grid(row=i, column=0, sticky="w", pady=2)
            widget = (ttk.Combobox(fields, state="readonly", textvariable=var, values=["max", "min"])
                      if key == "direction" else ttk.Entry(fields, textvariable=var))
            widget.grid(row=i, column=1, sticky="ew", pady=2)

        unit = tk.StringVar()
        unit_row = len(params) + 1
        ttk.Label(fields, text="Единица").grid(row=unit_row, column=0, sticky="w")
        unit_box = ttk.Combobox(fields, state="readonly", textvariable=unit, width=10)
        unit_box.grid(row=unit_row, column=1, sticky="w", pady=3)
        displayed = {"unit": None}
        time_units = {"s": 1, "с": 1, "ms": 1000, "мс": 1000}
        def unit_changed(event=None):
            old, new = displayed["unit"], unit.get()
            if old in time_units and new in time_units and old != new:
                for key in ("low", "high", "min", "max"):
                    field = variables.get(key)
                    if field and field.get().strip():
                        try:
                            val = Fraction(field.get().strip().replace(",", ".")) * Fraction(time_units[new], time_units[old])
                        except (ValueError, ZeroDivisionError):
                            unit.set(old)
                            messagebox.showerror("Единица", "Введите число в текущей единице.", parent=win)
                            return
                        field.set(str(val))
            displayed["unit"] = new
        unit_box.bind("<<ComboboxSelected>>", unit_changed)

        def metric_defaults(event=None):
            sid = options[metric.get()]
            native = "%" if sid.endswith("__rel") else self.catalog["stats"][sid]["unit"]
            choices = [native]
            if native in time_units:
                choices += [("ms" if native == "s" else "мс") if time_units[native] == 1
                            else ("s" if native == "ms" else "с")]
            unit_box.configure(values=choices)
            unit.set(native)
            displayed["unit"] = native
            if not criterion:
                return
            spec = self.catalog["stats"][sid.removesuffix("__rel")]
            scale = spec.get("scale", {}) if not sid.endswith("__rel") else {}
            variables["direction"].set(spec["direction"])
            for key in ("low", "high"):
                variables[key].set(str(scale.get(key, "")))
        combo.bind("<<ComboboxSelected>>", metric_defaults)
        metric_defaults()
        if edit:
            for key, _, default in params:
                variables[key].set(str(rule.get(key, default)))
            if rule.get("unit"):
                unit.set(rule["unit"])
                displayed["unit"] = unit.get()

        def commit():
            next_rule = {"stat": options[metric.get()], "unit": unit.get()}
            try:
                for key, field in variables.items():
                    value = field.get().strip().replace(",", ".")
                    if value:
                        if key != "direction":
                            num = Fraction(value)
                            if key == "weight" and num < 0:
                                raise ValueError("Вес должен быть неотрицательным.")
                        next_rule[key] = value
                if not criterion and "min" not in next_rule and "max" not in next_rule:
                    raise ValueError("Введите хотя бы один предел.")
                if "min" in next_rule and "max" in next_rule and Fraction(next_rule["min"]) > Fraction(next_rule["max"]):
                    raise ValueError("Минимум больше максимума.")
                if criterion and any(i != idx and r["stat"] == next_rule["stat"] for i, r in enumerate(seq)):
                    raise ValueError("Эта цель уже добавлена.")
            except (ValueError, ZeroDivisionError) as exc:
                messagebox.showerror("Настройки подбора", str(exc), parent=win)
                return
            if edit:
                seq[idx] = next_rule
            else:
                seq.append(next_rule)
            self.invalidate_search()
            self.render_rules()
            win.destroy()
        ttk.Button(fields, text="Применить", command=commit).grid(row=len(params)+2, column=1, sticky="e", pady=5)
        win.grab_set()

    def invalidate_search(self):
        self.token, self.cancel = self.requests.new()
        self.report, self.latest_query = None, None
        self._wipe(self.results)
        self._write(self.detail, "")
        self.search_status.set("Готов к поиску.")

    def start_search(self):
        if not self.catalog or self.wid not in self.catalog["weapons"]:
            return
        try:
            k = int(self.top_k.get())
            if not 1 <= k <= 20:
                raise ValueError("Размер топа должен быть от 1 до 20.")
            mode = "single" if self.mode.get() == "Одна цель" else "weighted"
            criteria = copy.deepcopy(self.criteria)
            if mode == "weighted":
                positive = [c for c in criteria if Fraction(str(c.get("weight", 1))) > 0]
                if len(positive) == 1 and messagebox.askyesno("Один положительный вес",
                        "Запустить одиночный подбор по этому показателю?", parent=self.root):
                    mode, criteria = "single", positive
            if mode == "single" and len(criteria) != 1:
                raise ValueError("Для одиночного подбора оставьте одну цель.")
            query = {"weapon_id": self.wid, "mode": mode, "criteria": criteria, "k": k,
                     "thresholds": copy.deepcopy(self.thresholds), "locks": copy.deepcopy(self.locks),
                     "context": copy.deepcopy(self.context), "profile": self.profile,
                     "revision": self.catalog["revision"], "calc_version": self.catalog["calc_version"]}
            # Validate user input synchronously, before a worker can replace a previous result.
            _search_query(self.catalog, query)
        except (ValueError, TypeError, ZeroDivisionError) as exc:
            messagebox.showerror("Настройки подбора", str(exc), parent=self.root)
            return
        self.invalidate_search()
        self.latest_query = query
        token, event, engine = self.token, self.cancel, self.engine
        self.search_status.set("Лучшие из проверенных · начат расчёт")
        def search_worker():
            try:
                report = run_search(engine, query, cancel=event,
                                progress=lambda r: self.events.put((token, r, None)))
                self.events.put((token, report, None))
            except Exception as exc:
                self.events.put((token, None, str(exc)))
        threading.Thread(target=search_worker, name="Bodycam search", daemon=True).start()

    def cancel_search(self):
        if self.cancel:
            self.cancel.set()

    def poll_search(self):
        if self._closing:
            return
        try:
            while True:
                token, report, error = self.events.get_nowait()
                if not self.requests.current(token):
                    continue
                if error:
                    self.search_status.set("Поиск остановлен. " + error)
                    self.notice.set(error)
                elif report is not None:
                    self.apply_report(token, report)
        except queue.Empty:
            pass
        self._pending_event = self.root.after(120, self.poll_search)

    def apply_report(self, token, report):
        if not self.requests.current(token):
            return False
        selected = self.results.selection()
        self.report = report
        self._wipe(self.results)
        q = report["query"]
        proof = "Топ подтверждён" if report["complete"] else "Лучшие из проверенных"
        outcome = _OUTCOMES.get(report["outcome"], report["outcome"])
        self.search_status.set(f'{proof} · {outcome} · проверено {report["evaluated"]} / {report["total"]}'
                               f' · рев. {q.get("revision", self.catalog["revision"])} · по опубликованным эффектам')
        if report.get("pruned"):
            self.search_status.set(self.search_status.get() + f' · доказательно исключено {report["pruned"]}')
        current = (self.evaluation or {}).get("effective", {})
        for i, r in enumerate(report["rows"]):
            diff = [self._slot_label(s) + " → " + self._module_name(mid) for s, mid in r["effective"].items()
                    if current.get(s) != mid]
            self.results.insert("", "end", iid=str(i), values=(i+1, _ui_number(r["value"], 3),
                r["changes"], ", ".join(diff) or "Совпадает с редактором"))
        if report["rows"]:
            idx = selected[0] if selected and self.results.exists(selected[0]) else "0"
            self.results.selection_set(idx)
        self.result_detail()
        return True

    def _selected_row(self):
        ids = self.results.selection()
        if self.report and ids and int(ids[0]) < len(self.report["rows"]):
            return self.report["rows"][int(ids[0])]

    def result_detail(self):
        row = self._selected_row()
        if not row:
            report = self.report or {}
            counts = report.get("excluded", {})
            labels = {"threshold": "за пределами порогов", "unknown_metric": "неизвестная метрика",
                      "unknown": "нет подтверждения установки", "conflict": "конфликт"}
            lines = [f"{labels.get(key, key)} — {count}" for key, count in counts.items() if count]
            lines.extend(report.get("reasons", []))
            self._write(self.detail, "\n".join(lines))
            return
        q = self.report["query"]
        goals = ", ".join(f'{self._metric_title(c["stat"], c.get("unit"))} · {c.get("direction", "")}' for c in q["criteria"])
        lines = [f"Цель — {goals}", f'Результат — {_ui_number(row["value"], 4)}']
        for t in q.get("thresholds", []):
            lines.append(f'Предел · {self._metric_title(t["stat"], t.get("unit"))} от {t.get("min", "−∞")} до {t.get("max", "+∞")}')
        for c in row.get("components", []):
            if "u" not in c:
                lines.append(f'{self._metric_title(c["stat"])} · {_ui_number(c["value"], 4)} {c["unit"]}')
                continue
            lines.append(f'{self._metric_title(c["stat"])} · {_ui_number(c["value"], 4)} {c["unit"]} · '
                         f'{c["direction"]}, [{c["low"]}, {c["high"]}] · u={_ui_number(c["u"], 4)} · '
                         f'доля {_ui_number(100*c["share"], 2)}% · вклад {_ui_number(c["contribution"], 4)}'
                         + (" · за ориентирами" if c["outside"] else ""))
        lines += ["", "Состав"] + [self._slot_label(s) + " — " + self._module_name(v) for s, v in row["effective"].items()]
        self._write(self.detail, "\n".join(lines))

    def _result_snapshot(self):
        row = self._selected_row()
        if row is None:
            self.notice.set("Выберите найденную сборку.")
            return None
        q = self.report["query"]
        title = self.catalog["weapons"][self.wid]["name"] + " · подбор"
        return make_snapshot(self.engine, self.wid, row["actions"], q.get("locks", {}), q.get("context", {}),
                             title, q, {**row, "complete": self.report["complete"],
                                           "evaluated": self.report["evaluated"], "outcome": self.report["outcome"]})

    def edit_result(self):
        snap = self._result_snapshot()
        if snap and self.may_leave():
            self._load_editor(snap["weapon_id"], snap["actions"], snap["locks"], snap["context"], origin=snap)
            self.dirty = True
            self.refresh_editor()
            self.nb.select(self.pages["Конструктор"])

    def save_result(self):
        snap = self._result_snapshot()
        if snap:
            self._save_as(snap)

    def compare_result(self):
        snap = self._result_snapshot()
        if snap:
            self.add_snapshot(snap)

    def current_snapshot(self):
        if not self.catalog or self.wid not in self.catalog["weapons"]:
            return None
        if self.context_error:
            messagebox.showerror("Условия расчёта", self.context_error, parent=self.root)
            return None
        source = self.origin or {}
        title = (self.preset or {}).get("name", self.catalog["weapons"][self.wid]["name"])
        snap = make_snapshot(self.engine, self.wid, self.actions, self.locks, self.context, title,
                             source.get("query"), source.get("result"))
        if self.preset:
            for key in ("migration", "previous_names", "previous_catalog_hash", "previous_result",
                        "previous_query"):
                if key in self.preset:
                    snap[key] = copy.deepcopy(self.preset[key])
            archived = self.preset.get("names", {})
            for key in ("slots", "modules"):
                labels = dict(archived.get(key, {}))
                for uid, label in snap["names"][key].items():
                    if uid not in labels or label != uid:
                        labels[uid] = label
                snap["names"][key] = labels
            slots = self.catalog["weapons"][self.wid]["slots"]
            snap["lost_slots"] = {
                sid: mid for sid, mid in self.preset.get("lost_slots", {}).items()
                if sid in self.actions and (sid not in slots or
                    (mid not in self.catalog["modules"] and _ui_action(self.actions[sid]) == mid))
            }
        return snap

    def compare_editor(self):
        snap = self.current_snapshot()
        if snap:
            self.add_snapshot(snap)

    def _build_compare(self):
        page = self.pages["Сравнение"]
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(0, 5))
        ttk.Label(controls, text="Опора").pack(side="left", padx=(0, 5))
        self.ref = ttk.Combobox(controls, width=24, state="readonly")
        self.ref.pack(side="left", padx=(0, 5))
        self.ref.bind("<<ComboboxSelected>>", lambda e: self.render_comparison())
        self.diff_only = tk.BooleanVar()
        ttk.Checkbutton(controls, text="Только различия", variable=self.diff_only,
                        command=self.render_comparison).pack(side="left", padx=4)
        self._button(controls, "Пересчитать копию опоры", self.recalculate_reference)
        self._button(controls, "Убрать опору", self.remove_reference)
        self.comp_canvas = tk.Canvas(page, background="#fafbfc", highlightthickness=0)
        sy = ttk.Scrollbar(page, orient="vertical", command=self.comp_canvas.yview)
        sx = ttk.Scrollbar(page, orient="horizontal", command=self.comp_canvas.xview)
        self.comp_canvas.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sx.pack(fill="x", side="bottom")
        sy.pack(fill="y", side="right")
        self.comp_canvas.pack(fill="both", expand=True)
        self.matrix = ttk.Frame(self.comp_canvas, padding=3)
        self.comp_canvas.create_window((0, 0), window=self.matrix, anchor="nw")
        self.matrix.bind("<Configure>", lambda e: self.comp_canvas.configure(
            scrollregion=self.comp_canvas.bbox("all")))
        self.render_comparison()

    def add_snapshot(self, snap):
        if len(self.snapshots) == 4:
            titles = "\n".join(f'{i+1}. {s["title"]}' for i, s in enumerate(self.snapshots))
            number = simpledialog.askinteger("Заменить снимок", "Заняты четыре столбца. Какой заменить?\n" + titles,
                                             minvalue=1, maxvalue=4, parent=self.root)
            if number is None:
                return
            self.snapshots[number-1] = copy.deepcopy(snap)
        else:
            self.snapshots.append(copy.deepcopy(snap))
        self.render_comparison()
        self.nb.select(self.pages["Сравнение"])

    def remove_reference(self):
        idx = self.ref.current()
        if 0 <= idx < len(self.snapshots):
            del self.snapshots[idx]
            self.render_comparison()

    def recalculate_reference(self):
        idx = self.ref.current()
        if idx < 0 or not self.active_catalog:
            return
        snap = self.store.migrate(self.snapshots[idx], self.active_catalog)
        snap["title"] = self.snapshots[idx]["title"] + " · пересчёт"
        self.add_snapshot(snap)

    def render_comparison(self):
        reference = max(0, min(self.ref.current(), len(self.snapshots)-1))
        for child in self.matrix.winfo_children():
            child.destroy()
        titles = [f'{i+1}. {s["title"]}' for i, s in enumerate(self.snapshots)]
        self.ref.configure(values=titles)
        if not titles:
            self.ref.set("")
            ttk.Label(self.matrix, text="Добавьте снимки из конструктора, подбора или пресетов.").grid(
                row=0, column=0, sticky="w", pady=6)
            return
        self.ref.current(reference)
        header = ["Показатель / слот"] + [
            f'{t}\n{s["game_version"]} · рев. {s["revision"]}\n{_GUI_STATES.get(s.get("status"), "")}'
            for t, s in zip(titles, self.snapshots)]
        rows = compare_snapshots(self.snapshots, reference, self.diff_only.get())
        for col, label in enumerate(header):
            tk.Label(self.matrix, text=label, anchor="w", justify="left", padx=8, pady=5,
                     wraplength=220, bg="#203044", fg="white").grid(row=0, column=col, sticky="nsew", padx=1)
        for i, row in enumerate(rows, 1):
            color = "#f1f4f6" if i % 2 else "#ffffff"
            for j, label in enumerate([row["name"]] + row["cells"]):
                winner = j > 0 and j-1 in row["leaders"]
                cell = tk.Label(self.matrix, text=label, anchor="w", justify="left", padx=7, pady=3,
                                wraplength=225, fg="#125f49" if winner else "#26333b",
                                bg="#ddf2e9" if winner else color)
                cell.grid(row=i, column=j, sticky="nsew", padx=1)
                cell.bind("<MouseWheel>", lambda e: self.comp_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
                cell.bind("<Button-4>", lambda e: self.comp_canvas.yview_scroll(-1, "units"))
                cell.bind("<Button-5>", lambda e: self.comp_canvas.yview_scroll(1, "units"))

    def _build_presets(self):
        page = self.pages["Пресеты"]
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        for label, cmd in [("Создать из редактора…", self.new_preset),
                           ("Открыть", self.open_selected), ("В сравнение", self.compare_preset),
                           ("Переименовать…", self.rename_selected), ("Копировать…", self.copy_selected),
                           ("Удалить…", self.delete_selected), ("Обновить список", self.refresh_presets)]:
            self._button(bar, label, cmd)
        bottom = ttk.Frame(page)
        bottom.pack(side="bottom", fill="x")
        self._button(bottom, "Перенести копию в рабочую ревизию…", self.migrate_selected)
        panes = ttk.Panedwindow(page, orient="vertical")
        panes.pack(fill="both", expand=True)
        a, b = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(a, weight=2)
        panes.add(b, weight=2)
        self.presets = self._tree(a, [("name", "Пресет", 170), ("model", "Модель", 130),
            ("rev", "Игра / ревизия", 108), ("state", "Состояние", 110), ("date", "Изменён (UTC)", 170)], height=5)
        self.preset_details = self._text_box(b, 5)
        self.presets.bind("<Double-1>", lambda e: self.open_selected())
        self.presets.bind("<<TreeviewSelect>>", lambda e: self.show_preset())

    def refresh_presets(self, selected=None):
        old = self.presets.selection()
        self._wipe(self.presets)
        self.saved = {p["uuid"]: p for p in self.store.list_presets()}
        for uid, p in self.saved.items():
            state = p.get("status", "unknown")
            self.presets.insert("", "end", iid=uid, values=(p["name"], p.get("names", {}).get("weapon", p["weapon_id"]),
                f'{p["game_version"]} / {p["revision"]}', _GUI_STATES.get(state, state),
                p["updated_at"].split(".")[0].replace("T", " ")), tags=(state,))
        key = selected or (old[0] if old else None)
        if key and key in self.saved:
            self.presets.selection_set(key)
        self.show_preset()

    def selected_preset(self):
        selected = self.presets.selection()
        return self.saved.get(selected[0]) if selected else None

    def show_preset(self):
        p = self.selected_preset()
        if p is None:
            self._write(self.preset_details, "Выберите пресет для просмотра состава и условий.")
            return
        n = p.get("names", {})
        lines = [p["name"], n.get("weapon", p["weapon_id"]) + "   ·   " + str(p["uuid"]),
                 f'Игра {p["game_version"]} · рев. {p["revision"]} · расчёт {p["calc_version"]}',
                 _GUI_STATES.get(p.get("status"), ""), *p.get("diagnostics", []), ""]
        for sid, act in p.get("actions", {}).items():
            mid = p.get("effective", {}).get(sid)
            if mid is None and _ui_action(act) not in ("stock", "empty"):
                mid = _ui_action(act)
            name = n.get("modules", {}).get(mid, mid) if mid else "Пусто"
            mode = {"stock": "штатно", "empty": "снято"}.get(_ui_action(act), "выбрано")
            lines.append(f'{n.get("slots", {}).get(sid, sid)} — {name} ({mode})'
                         + (" · закреплено" if sid in p.get("locks", {}) else ""))
        if p.get("context"):
            lines.extend(["", "Условия", _ui_json(p["context"])])
        if p.get("query"):
            r = p.get("result") or {}
            lines.extend(["", "Подбор · " + ("топ подтверждён" if r.get("complete") else "лучшие из проверенных"),
                          "Значение — " + str(r.get("value", "—")), _ui_json(p["query"])])
        if p.get("migration"):
            lines.extend(["", "Перенос", *p["migration"]])
        self._write(self.preset_details, "\n".join(lines))

    def _save_as(self, data, initial=None, *, name=None):
        default = initial or data.get("name") or data.get("title") or "Сборка"
        while True:
            if name is None:
                name = simpledialog.askstring("Сохранить пресет", "Имя пресета", initialvalue=default, parent=self.root)
                if name is None:
                    return None
            name = name.strip()
            if not name:
                messagebox.showinfo("Имя пресета", "Введите непустое имя.", parent=self.root)
                name = None
                continue
            try:
                try:
                    saved = self.store.save({**data, "title": name}, name)
                except PresetNameError as collision:
                    answer = messagebox.askyesnocancel("Имя занято", f'Заменить «{collision.existing["name"]}»?\n'
                            "Да — заменить, Нет — указать другое имя.", parent=self.root)
                    if answer is None:
                        return None
                    if not answer:
                        name = None
                        continue
                    saved = self.store.save({**data, "title": name}, name, replace=collision.existing)
                self.refresh_presets(saved["uuid"])
                self.notice.set(f'Сохранён пресет «{saved["name"]}». ' + self.store.notice)
                return saved
            except StalePresetError as exc:
                answer = messagebox.askyesnocancel("Пресет изменился", str(exc) +
                    "\nДа — сохранить свою копию, Нет — перечитать запись.", parent=self.root)
                if answer is True:
                    data = {k: v for k, v in data.items() if k not in ("uuid", "edit_version", "created_at", "updated_at")}
                    default, name = name + " (копия)", None
                    continue
                if answer is False:
                    self.refresh_presets(data.get("uuid"))
                    new = self.saved.get(data.get("uuid"))
                    if new:
                        self.open_preset(new, check_dirty=False)
                return None
            except Exception as exc:
                messagebox.showerror("Сохранение", str(exc), parent=self.root)
                return None

    def save_editor(self):
        snap = self.current_snapshot()
        if snap is None:
            return False
        if self.preset:
            snap = {**self.preset, **snap}
        saved = self._save_as(snap, name=self.preset.get("name") if self.preset else None)
        if saved:
            self.preset, self.dirty = saved, False
            self.refresh_editor()
        return bool(saved)

    def new_preset(self):
        snap = self.current_snapshot()
        if snap:
            self._save_as(snap)

    def open_selected(self):
        p = self.selected_preset()
        if p:
            self.open_preset(p)

    def open_preset(self, p, check_dirty=True):
        if check_dirty and not self.may_leave():
            return False
        digest = p["catalog_hash"]
        try:
            cat = self.catalog if self.catalog and digest == self.hash else self.store.archive(digest)
        except ValueError as exc:
            self.notice.set(str(exc))
            cat = None
        if not cat or p["weapon_id"] not in cat["weapons"]:
            self._viewer("Читаемый черновик · " + p["name"],
                         "Исходная модель или ревизия недоступна. Сохранённый снимок\n\n" + _ui_json(p))
            return False
        if self.hash != digest:
            self._use_catalog(cat, digest)
        self._load_editor(p["weapon_id"], p.get("actions"), p.get("locks"), p.get("context"), p, p)
        self._restore_query(p.get("query"))
        self.nb.select(self.pages["Конструктор"])
        self.notice.set("Открыт пресет «" + p["name"] + "». " +
                        ("Архивная ревизия." if self.hash != self.active_hash else "Рабочая ревизия."))
        return True

    def _restore_query(self, query):
        if not query:
            return
        self.mode.set("Одна цель" if query["mode"] == "single" else "По весам")
        self.top_k.set(str(query["k"]))
        self.criteria = copy.deepcopy(query["criteria"])
        self.thresholds = copy.deepcopy(query.get("thresholds", []))
        self.render_rules()
        self.search_status.set("Восстановлены условия сохранённого подбора.")

    def compare_preset(self):
        p = self.selected_preset()
        if p:
            self.add_snapshot({**p, "title": p["name"]})

    def rename_selected(self):
        p = self.selected_preset()
        if not p:
            return
        saved = self._save_as(p, p["name"])
        if saved and self.preset and self.preset["uuid"] == p["uuid"]:
            self.preset = saved

    def copy_selected(self):
        p = self.selected_preset()
        if p:
            data = {k: v for k, v in p.items() if k not in ("uuid", "edit_version", "created_at", "updated_at")}
            self._save_as(data, p["name"] + " (копия)")

    def delete_selected(self):
        p = self.selected_preset()
        if not p or not messagebox.askyesno("Удалить пресет", "Удалить «" + p["name"] + "»?", parent=self.root):
            return
        try:
            self.store.delete(p)
        except Exception as exc:
            messagebox.showerror("Удаление", str(exc), parent=self.root)
            self.refresh_presets()
            return
        if self.preset and p["uuid"] == self.preset["uuid"]:
            self.preset = None
        self.refresh_presets()
        self.notice.set("Пресет удалён.")

    def migrate_selected(self):
        p = self.selected_preset()
        if not p or self.active_catalog is None:
            return
        moved = self.store.migrate(p, self.active_catalog)
        notes = moved.get("migration", [])
        title = f'Перенос в {moved["game_version"]} · рев. {moved["revision"]}'
        win = self._viewer(title, "\n".join(notes) or "Все детали доступны. Расчётные значения совпадают.")
        btn = ttk.Button(win, text="Сохранить копию…", command=lambda: self._save_as(
            moved, p["name"] + f' (рев. {moved["revision"]})'))
        btn.pack(pady=3)

    def close(self):
        if not self.may_leave():
            return
        self._closing = True
        self.cancel_search()
        if self._pending_event is not None:
            self.root.after_cancel(self._pending_event)
        self.store.close()
        self.root.destroy()


def run_gui(catalog_path, db_path=None):
    import tkinter as tk
    root = tk.Tk()
    BodycamApp(root, catalog_path, db_path)
    root.mainloop()
    return 0


def _test_query(wid="demo", stat="index", k=2):
    return {"weapon_id": wid, "criteria": [{"stat": stat}], "k": k}


def tests_integration():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as folder:
        path = Path(folder) / "изменяемый справочник.json"
        db = Path(folder) / "пользователь.sqlite3"
        store = Store(db)
        original = demo_catalog()
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        first = load_catalog(path)
        engine = Engine(first)
        store.activate(first)
        before = run_search(engine, _test_query())
        assert before["complete"] and [r["value"] for r in before["rows"]] == [12, 10]
        assert [r["effective"]["a"] for r in before["rows"]] == ["m", None]
        chosen = before["rows"][0]
        snap = make_snapshot(engine, "demo", chosen["actions"], ["a"], {}, "Первая ревизия",
                             before["query"], {**chosen, "complete": before["complete"]})
        preset = store.save(snap, "Архивный результат")
        changed = deepcopy(first)
        changed["revision"] += 1
        changed["modules"]["m"]["effects"][0]["value"] = -1
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        store.activate(load_catalog(path))
        rows = run_search(Engine(store.current()), _test_query())["rows"]
        assert [(r["effective"]["a"], r["value"]) for r in rows] == [(None, 10), ("m", 9)]
        archived = store.archive(preset["catalog_hash"])
        assert Engine(archived).evaluate("demo", preset["actions"])["metrics"]["index"]["value"] == 12
        assert preset["pins"] == {"a": chosen["actions"]["a"]} and preset["result"]["complete"]
        blocked = deepcopy(changed)
        blocked["revision"] += 1
        blocked["compatibility"]["demo_m"]["status"] = "forbidden"
        blocked["compatibility"]["demo_m"]["reason"] = "Запрет из фикстуры А2"
        store.activate(blocked)
        migrated = store.migrate(preset, blocked)
        red = Engine(blocked).evaluate("demo", migrated["actions"])
        assert red["status"] == "conflict" and not red["metrics"]
        only = run_search(Engine(blocked), _test_query())
        assert only["complete"] and [r["effective"]["a"] for r in only["rows"]] == [None]
        broken = deepcopy(blocked)
        broken["revision"] += 1
        broken["compatibility"]["demo_m"]["module_id"] = "missing-module"
        try:
            store.activate(broken)
        except ValueError as err:
            assert "module_id" in str(err)
        else:
            raise AssertionError("Принят потерянный module_id")
        assert store.current() == blocked
        assert store.get(preset["uuid"])["metrics"]["index"]["value"] == "12"

        expanded = deepcopy(changed)
        expanded["revision"] += 3
        expanded["weapons"]["empty-model"] = deepcopy(expanded["weapons"]["demo"])
        expanded["weapons"]["empty-model"]["name"] = "Модель без пары"
        expanded["modules"]["n"] = deepcopy(expanded["modules"]["m"])
        expanded["modules"]["n"]["effects"][0]["value"] = 5
        expanded["compatibility"]["demo_n"] = dict(expanded["compatibility"]["demo_m"], module_id="n")
        path.write_text(json.dumps(expanded, ensure_ascii=False), encoding="utf-8")
        extended_engine = Engine(load_catalog(path))
        assert extended_engine.evaluate("empty-model", {"a": "m"})["status"] == "unknown"
        assert run_search(extended_engine, _test_query())["rows"][0]["value"] == 15
        assert run_search(extended_engine, _test_query())["rows"][0]["effective"]["a"] == "n"
        store.close()
    return {"А1": "Фикстура через JSON и SQLite: m=12, пусто=10; следующая ревизия 10, 9; архив даёт 12.",
            "А2": "Копия с запрещённым m красная; топ содержит только пустой слот; битая ссылка отклонена, активна ревизия 3.",
            "Д2-ядро": "Новый JSON: модель без пары получает unknown; новый n даёт 15 вместо 10."}


def tests_comparison(real=None):
    fixture = demo_catalog()
    engine = Engine(fixture)
    snap = make_snapshot(engine, "demo", {}, {}, {}, "Снимок")
    a, b, c = (deepcopy(snap) for _ in range(3))
    for s, v in zip((a, b, c), ("0", None, "0")):
        s["metrics"]["index"].update(value=v, state="unknown" if v is None else "known")
    row = next(r for r in compare_snapshots([a, b, c]) if r["id"] == "index")
    assert row["leaders"] == [0, 2] and "Неизвестно" == row["cells"][1]
    assert "%" not in row["cells"][2]
    a["metrics"]["index"]["value"], c["metrics"]["index"]["value"] = "1.004", "1.003"
    rows = compare_snapshots([a, c], differences=True)
    assert any(r["id"] == "index" and r["leaders"] == [0] for r in rows)
    assert format_exact("1.005", 2) == "1,00" and format_exact("1.015", 2) == "1,02"
    snap2 = make_snapshot(engine, "demo", {"a": "m"}, {}, {}, "Другой")
    original = json.dumps(snap2, sort_keys=True)
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "snap.sqlite3")
        store.activate(fixture)
        saved = store.save(snap2, "Исходник")
        saved = store.rename(saved, "Отредактирован")
        store.delete(saved)
        store.close()
    assert json.dumps(snap2, sort_keys=True) == original
    evidence = {"П1-фикстура": "Лидеры в строке 0 / unknown / 0: 1 и 3; 1,004 и 1,003 различаются; удаление пресета сохраняет снимок."}
    if real:
        e = Engine(real)
        factory = make_snapshot(e, "sg5_k", {}, {}, {}, "SG5-K")
        modified = make_snapshot(e, "sg5_k", {"forearm":"m94", "muzzle":"m116",
                                 "magazine":"m428", "underbarrel":"empty"}, {}, {}, "SG5-K · 15")
        other = make_snapshot(e, "m4", {}, {}, {}, "M4")
        rows = {r["id"]:r for r in compare_snapshots([factory, modified, other])}
        ads = rows["ads_speed__rel"]
        assert "п.п." in ads["cells"][1] and "справочно" in ads["cells"][2], ads
        assert "Δ" in rows["capacity"]["cells"][2]
        health = make_snapshot(e, "sg5_k", {}, {}, {"health": 150}, "SG5-K · H=150")
        ctx_rows = {r["id"]: r for r in compare_snapshots([factory, health])}
        assert "справочно" in ctx_rows["ttk"]["cells"][1], ctx_rows["ttk"]
        assert "Δ" in ctx_rows["capacity"]["cells"][1]
        evidence["П1"] = "Два SG5-K и M4: ADS в п.п. у общей базы, M4 справочно; H=150 отключает Δ TTK, сохраняет Δ ёмкости."
    return evidence


def benchmark_desktop():
    """Повторяемый стенд Р4. Временная БД, настоящее окно Tk и рабочий поток подбора."""
    import platform
    import statistics
    import time
    import tkinter as tk

    with tempfile.TemporaryDirectory(prefix="Bodycam-R4-") as folder:
        root = tk.Tk()
        app = BodycamApp(root, Path(__file__).with_name("bodycam_catalog.json"),
                         Path(folder) / "presets.sqlite3")
        try:
            app.select_weapon("sg5_k")
            app.slot = "forearm"
            edits = []
            for i in range(22):
                started = time.perf_counter()
                app.set_action("forearm", "m94" if i % 2 else "stock")
                root.update()
                if i >= 2:
                    edits.append(1000 * (time.perf_counter() - started))

            app.criteria = [{"stat": "dps", "direction": "max"}]
            app.start_search()
            began = time.perf_counter()
            updates, previous = [began], -1
            while time.perf_counter() - began < 2:
                root.update()
                checked = (app.report or {}).get("evaluated", -1)
                if checked > previous:
                    updates.append(time.perf_counter())
                    previous = checked
                time.sleep(.008)

            cancel_at = time.perf_counter()
            app.cancel_search()
            while not (app.report and app.report["cancelled"]):
                root.update()
                if time.perf_counter() - cancel_at > 8:
                    raise RuntimeError("Измерение отмены не завершилось за 8 с")
                time.sleep(.008)
            cancel_ms = 1000 * (time.perf_counter() - cancel_at)
            marks = updates + [cancel_at]
            max_gap = max(b - a for a, b in zip(marks, marks[1:]))
            elapsed = time.perf_counter()
            objective = run_search(app.engine, {"weapon_id": "sg5_k", "k": 5,
                                   "criteria": [{"stat": "ads_speed__rel", "direction": "max"}]})
            real_seconds = time.perf_counter() - elapsed
            record = {
                "os": platform.platform(), "python": platform.python_version(),
                "tk": root.tk.call("info", "patchlevel"),
                "logical_cpus_reported_by_os": os.cpu_count(),
                "revision": app.catalog["revision"], "edit_median_ms": round(statistics.median(edits), 3),
                "progress_max_gap_s": round(max_gap, 3), "cancel_ms": round(cancel_ms, 3),
                "evaluated_before_cancel": app.report["evaluated"],
                "synthetic_top20": benchmark_search(),
                "sg5_k_top5": {"seconds": round(real_seconds, 6), "total": objective["total"],
                               "evaluated": objective["evaluated"], "complete": objective["complete"]},
            }
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return record
        finally:
            app.dirty = False
            app.close()


# Команды рассчитаны на запуск из любой рабочей директории.
import argparse
from pathlib import Path
import sys
import tempfile
import traceback


def _catalog_changes(previous, candidate, path=""):
    """Stream all differences, including fields with no counterpart in Excel."""
    if isinstance(previous, dict) and isinstance(candidate, dict):
        for key in sorted(previous.keys() | candidate.keys()):
            # RFC 6901 JSON Pointer.
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in previous:
                yield {"operation": "add", "path": child, "new": candidate[key]}
            elif key not in candidate:
                yield {"operation": "remove", "path": child, "old": previous[key]}
            else:
                yield from _catalog_changes(previous[key], candidate[key], child)
    elif isinstance(previous, list) and isinstance(candidate, list) and len(previous) == len(candidate):
        for i, (old, new) in enumerate(zip(previous, candidate)):
            yield from _catalog_changes(old, new, path + "/" + str(i))
    elif previous != candidate or isinstance(previous, bool) != isinstance(candidate, bool):
        yield {"operation": "replace", "path": path, "old": previous, "new": candidate}


def _import_command(path, external):
    try:
        import openpyxl
    except ImportError:
        raise ValueError("Для --import-xlsx нужен openpyxl. Установите: python -m pip install openpyxl") from None
    old = None
    if external.is_file():
        try:
            old = load_catalog(external)
        except (OSError, ValueError) as exc:
            print(f"Внешний каталог непригоден для сравнения: {exc}", file=sys.stderr)
    # Assemble in an otherwise empty private directory. Publish only a validated candidate.
    folder = Path(tempfile.mkdtemp(prefix="bodycam-import-", dir=external.parent))
    try:
        catalog = import_xlsx(path, active=old, report_path=folder / "cells.csv")
        validate_catalog(catalog)
        candidate = folder / "bodycam_catalog.candidate.json"
        with candidate.open("w", encoding="utf-8") as out:
            json.dump(catalog, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write("\n")
        # Verify the emitted representation as well as the in-memory candidate.
        load_catalog(candidate)
        delta = folder / "changes.jsonl"
        count = 0
        with delta.open("w", encoding="utf-8") as out:
            for change in _catalog_changes(old or {}, catalog):
                out.write(_canon(change) + "\n")
                count += 1
    except BaseException:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
    print(f"Кандидат: {candidate}\nЯчейки: {folder / 'cells.csv'}\nРазличия: {delta} ({count})")
    if old and count and (old["game_version"], old["revision"]) == (catalog["game_version"], catalog["revision"]):
        print("Перед активацией изменённого кандидата увеличьте revision.")
    return 0


def cli(argv=None):
    commands = argparse.ArgumentParser(description="Локальный игровой конфигуратор Bodycam")
    mode = commands.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="Проверить соседний JSON без записи в БД")
    mode.add_argument("--self-test", action="store_true", help="Запустить автономные встроенные проверки")
    mode.add_argument("--import-xlsx", metavar="КНИГА", type=Path, help="Создать отдельный кандидат и отчёт")
    args = commands.parse_args(argv)
    external = Path(__file__).resolve().with_name("bodycam_catalog.json")
    try:
        if args.validate:
            cat = load_catalog(external)
            print(f"Каталог {cat['game_version']}, ревизия {cat['revision']}. "
                  f"Моделей {len(cat['weapons'])}, деталей {len(cat['modules'])}, слотов {len(cat['slots'])}.")
            print(f"SHA-256: {fingerprint(cat)}")
        elif args.self_test:
            if not __debug__:
                raise ValueError("Запустите --self-test без -O: проверки используют assert.")
            # Synthetic tests also run when the program is copied without its catalog.
            real = None
            if external.is_file():
                try:
                    nearby = load_catalog(external)
                    if fingerprint(nearby) == _RELEASE_FINGERPRINT:
                        real = nearby
                except (ValueError, OSError) as exc:
                    print(f"Внешняя ревизия недоступна для дополнительных проверок: {exc}")
            for run in (tests_storage, lambda: tests_core(real), tests_search, tests_integration,
                        lambda: tests_comparison(real)):
                evidence = run()
                for key, description in evidence.items():
                    print(f"PASS {key} · {description}")
            if real is None:
                print("Проверки исходного игрового снимка пропущены: рядом нет эталонной ревизии 0.9.1.")
        elif args.import_xlsx:
            return _import_command(args.import_xlsx, external)
        else:
            run_gui(external, None)
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
