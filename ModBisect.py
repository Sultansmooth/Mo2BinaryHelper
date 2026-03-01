"""
MO2 Mod Bisect Tool v3
Find FPS-killing plugins via binary search.
Reads your full load order — no suspects file needed.
Syncs both plugins.txt (right pane) and modlist.txt (left pane).
"""

import os
import json
import struct
import glob
import shutil
from datetime import datetime

import mobase
from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QIcon, QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QTextEdit, QMessageBox, QApplication, QFrame,
    QSpinBox, QGroupBox,
)

# Base game plugins that should never be disabled
BASE_PLUGINS = {
    "fallout4.esm", "dlcrobot.esm", "dlcworkshop01.esm",
    "dlccoast.esm", "dlcworkshop02.esm", "dlcworkshop03.esm",
    "dlcnukaworld.esm",
}

# Mod folder name patterns to never disable (case-insensitive substring match).
# Keep this minimal — only mods the user specifically asked to exclude.
DEFAULT_EXCLUDE_PATTERNS = [
    "address library",
    "high fps",
    "addictol",
]


class BisectEngine:
    THRESHOLD = 1

    def __init__(self, profile_dir, mods_dir, overwrite_dir=None, exclude_patterns=None, organizer=None):
        self.profile_dir = profile_dir
        self.mods_dir = mods_dir
        self.overwrite_dir = overwrite_dir
        self.exclude_patterns = exclude_patterns or DEFAULT_EXCLUDE_PATTERNS
        self._organizer = organizer
        self.plugins_file = os.path.join(profile_dir, "plugins.txt")
        self.modlist_file = os.path.join(profile_dir, "modlist.txt")
        self.state_file = os.path.join(profile_dir, "bisect_state.json")
        self.plugins_backup = self.plugins_file + ".bisect_backup"
        self.modlist_backup = self.modlist_file + ".bisect_backup"
        self.log_file = os.path.join(profile_dir, "bisect_log.txt")
        self._plugin_to_mod = {}
        self._plugin_paths = {}  # plugin_lower -> file path (fallback only)

    @staticmethod
    def read_masters(plugin_path):
        masters = []
        try:
            with open(plugin_path, 'rb') as f:
                if f.read(4) != b'TES4':
                    return masters
                data_size = struct.unpack('<I', f.read(4))[0]
                f.read(16)
                end_pos = f.tell() + data_size
                while f.tell() < end_pos:
                    sub_type = f.read(4)
                    if len(sub_type) < 4:
                        break
                    sub_size = struct.unpack('<H', f.read(2))[0]
                    sub_data = f.read(sub_size)
                    if sub_type == b'MAST':
                        masters.append(sub_data.rstrip(b'\x00').decode('utf-8', errors='replace'))
        except Exception:
            pass
        return masters

    def get_all_known_plugins(self):
        """Get ALL plugins MO2 knows about, including disabled ones not in plugins.txt."""
        if self._organizer:
            try:
                return list(self._organizer.pluginList().pluginNames())
            except Exception:
                pass
        # Fallback: return what we can find on disk
        return list(self._plugin_paths.keys())

    def get_plugin_masters(self, plugin_name):
        """Get masters for a plugin. Uses MO2 API first, falls back to file reading."""
        if self._organizer:
            try:
                masters = list(self._organizer.pluginList().masters(plugin_name))
                if masters is not None:
                    return masters
            except Exception:
                pass
        # Fallback: read from file
        path = self._plugin_paths.get(plugin_name.lower())
        if path:
            return self.read_masters(path)
        return []

    def build_plugin_to_mod_map(self, all_plugins):
        """Build plugin -> mod mapping. Uses MO2 API first, falls back to folder scan."""
        if self._organizer:
            try:
                plugin_list = self._organizer.pluginList()
                mapping = {}
                for p in all_plugins:
                    origin = plugin_list.origin(p)
                    if origin:
                        mapping[p.lower()] = origin
                if mapping:
                    self._plugin_to_mod = mapping
                    return mapping
            except Exception:
                pass
        # Fallback: scan mod folders
        self._plugin_to_mod = self._scan_mod_folders()
        return self._plugin_to_mod

    def _scan_mod_folders(self):
        """Fallback: scan mod folders + overwrite for plugin files."""
        plugin_to_mod = {}
        for mod_folder in os.listdir(self.mods_dir):
            mod_path = os.path.join(self.mods_dir, mod_folder)
            if not os.path.isdir(mod_path):
                continue
            for ext in ('*.esp', '*.esm', '*.esl'):
                for p in glob.glob(os.path.join(mod_path, ext)):
                    name = os.path.basename(p).lower()
                    if name not in plugin_to_mod:
                        plugin_to_mod[name] = mod_folder
                    if name not in self._plugin_paths:
                        self._plugin_paths[name] = p
        if self.overwrite_dir and os.path.isdir(self.overwrite_dir):
            for ext in ('*.esp', '*.esm', '*.esl'):
                for p in glob.glob(os.path.join(self.overwrite_dir, ext)):
                    name = os.path.basename(p).lower()
                    if name not in self._plugin_paths:
                        self._plugin_paths[name] = p
        return plugin_to_mod

    def read_enabled_plugins(self):
        """Read only ENABLED plugins from plugins.txt (those with * prefix)."""
        plugins = []
        with open(self.plugins_file, "r", encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("*"):
                    plugins.append(line[1:])
                # Skip non-* entries — they're disabled, not part of bisection
        return plugins

    def _is_excluded_mod(self, mod_folder):
        """Check if a mod folder matches any exclude pattern."""
        folder_lower = mod_folder.lower()
        for pattern in self.exclude_patterns:
            if pattern.lower() in folder_lower:
                return True
        return False

    def classify_plugins(self, all_plugins):
        """Split plugins into base (always on) and testable (can be bisected).

        Base = game ESMs + excluded frameworks + all their masters (recursive).
        Testable = everything else.
        """
        all_lower = {p.lower(): p for p in all_plugins}

        # First pass: identify directly excluded plugins
        base_set = set(BASE_PLUGINS)
        excluded_mods = set()
        for p in all_plugins:
            if p.lower() in self._plugin_to_mod:
                mod_folder = self._plugin_to_mod[p.lower()]
                if self._is_excluded_mod(mod_folder):
                    base_set.add(p.lower())
                    excluded_mods.add(mod_folder)

        # Recursively exclude masters of excluded plugins (so they stay enabled too)
        changed = True
        while changed:
            changed = False
            for plugin_key in list(base_set):
                masters = self.get_plugin_masters(plugin_key)
                for m in masters:
                    if m.lower() in all_lower and m.lower() not in base_set:
                        base_set.add(m.lower())
                        changed = True

        base = []
        testable = []
        for p in all_plugins:
            if p.lower() in base_set:
                base.append(p)
            else:
                testable.append(p)
        return base, testable, excluded_mods

    def build_dependency_groups(self, testable):
        """Group testable plugins by dependencies. Returns (groups, cascade, deps, all_masters)."""
        testable_lower = {s.lower(): s for s in testable}
        deps = {}         # testable masters only (for grouping)
        all_masters = {}  # ALL masters per plugin (for missing master checks)
        not_found = []
        for s in testable:
            masters = self.get_plugin_masters(s)
            if masters is not None:
                all_masters[s.lower()] = [m.lower() for m in masters]
                suspect_masters = [m.lower() for m in masters if m.lower() in testable_lower]
                if suspect_masters:
                    deps[s.lower()] = suspect_masters
            else:
                not_found.append(s)

        # Cascade: plugins depending on 5+ other testable plugins
        cascade_keys = set()
        for key, masters in deps.items():
            if len(masters) >= 5:
                cascade_keys.add(key)

        filtered = [s for s in testable if s.lower() not in cascade_keys]
        cascade = [s for s in testable if s.lower() in cascade_keys]
        filtered_set = {s.lower() for s in filtered}

        # Union-Find
        parent = {}
        for s in filtered:
            parent[s.lower()] = s.lower()

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for pkey, masters in deps.items():
            if pkey in filtered_set:
                for m in masters:
                    if m in filtered_set:
                        union(pkey, m)

        groups = {}
        for s in filtered:
            root = find(s.lower())
            groups.setdefault(root, []).append(s)

        return list(groups.values()), cascade, deps, all_masters, not_found

    def close_under_masters(self, enabled_plugins, all_testable, all_masters, base_set):
        """Ensure all masters of enabled plugins are also enabled.

        Only pulls in masters from the testable set (plugins that were
        in plugins.txt). Won't enable plugins the user never had enabled.

        Returns (closed_list, pulled_in) where pulled_in are extra plugins
        that had to be added to satisfy dependencies.
        """
        testable_lower = {p.lower(): p for p in all_testable}
        enabled = {p.lower() for p in enabled_plugins}
        pulled_in = set()

        changed = True
        while changed:
            changed = False
            for plugin in list(enabled):
                for master in all_masters.get(plugin, []):
                    master_l = master.lower()
                    if master_l in base_set:
                        continue  # base plugin, always on
                    if master_l in testable_lower and master_l not in enabled:
                        enabled.add(master_l)
                        pulled_in.add(master_l)
                        changed = True

        closed = [testable_lower[p] for p in enabled if p in testable_lower]
        pulled_names = [testable_lower[p] for p in pulled_in if p in testable_lower]
        return closed, pulled_names

    def sync_modlist(self, enabled_plugins):
        """Sync modlist.txt left pane to match enabled plugins.

        Mods with at least one enabled plugin get +, others get -.
        Excluded framework mods and mods not in plugin_to_mod are left untouched.
        """
        enabled_lower = {p.lower() for p in enabled_plugins}

        enabled_mods = set()
        for plugin_key, mod_folder in self._plugin_to_mod.items():
            if plugin_key in enabled_lower:
                enabled_mods.add(mod_folder)

        # Only touch mod folders that contain testable (non-excluded) plugins
        testable_mods = set()
        for mod_folder in self._plugin_to_mod.values():
            if not self._is_excluded_mod(mod_folder):
                testable_mods.add(mod_folder)

        with open(self.modlist_file, "r") as f:
            lines = f.readlines()

        new_lines = []
        changed = 0
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                new_lines.append(line)
                continue
            prefix = s[0]
            mod_name = s[1:] if prefix in ('+', '-') else s
            if mod_name in testable_mods:
                if mod_name in enabled_mods:
                    new_line = "+" + mod_name + "\n"
                else:
                    new_line = "-" + mod_name + "\n"
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
            else:
                new_lines.append(line)

        with open(self.modlist_file, "w") as f:
            f.writelines(new_lines)
        return changed

    def order_plugins(self, groups, all_deps):
        """Topological sort so masters load before dependents."""
        enabled = set()
        for g in groups:
            for p in g:
                enabled.add(p.lower())
        in_deg = {p.lower(): 0 for g in groups for p in g}
        fwd = {p.lower(): [] for g in groups for p in g}
        for pkey, masters in all_deps.items():
            if pkey in enabled:
                for m in masters:
                    if m in enabled:
                        in_deg[pkey] = in_deg.get(pkey, 0) + 1
                        fwd.setdefault(m, []).append(pkey)
        queue = [k for k, v in in_deg.items() if v == 0]
        ordered = []
        while queue:
            node = queue.pop(0)
            ordered.append(node)
            for dep in fwd.get(node, []):
                in_deg[dep] -= 1
                if in_deg[dep] == 0:
                    queue.append(dep)
        case_map = {}
        for g in groups:
            for p in g:
                case_map[p.lower()] = p
        return [case_map[k] for k in ordered if k in case_map]

    def backup_files(self):
        for src, dst in [(self.plugins_file, self.plugins_backup),
                         (self.modlist_file, self.modlist_backup)]:
            if not os.path.exists(dst):
                shutil.copy2(src, dst)

    def restore_backups(self):
        # Restore modlist (left pane) FIRST so mods are enabled
        # before their plugins come back in plugins.txt
        for dst, src in [(self.modlist_file, self.modlist_backup),
                         (self.plugins_file, self.plugins_backup)]:
            if os.path.exists(src):
                shutil.copy2(src, dst)
                os.remove(src)
        if os.path.exists(self.state_file):
            os.remove(self.state_file)

    def write_plugins(self, base_plugins, enabled_testable):
        """Write plugins.txt preserving original order from backup.

        Keeps all base/excluded plugins, adds enabled testable plugins,
        removes disabled testable plugins. Original load order is preserved.
        Syncs left pane FIRST so mods are enabled before their plugins.
        """
        enabled_set = {p.lower() for p in enabled_testable}
        base_set = {p.lower() for p in base_plugins}

        # Sync left pane FIRST — enable mods before their plugins
        self.sync_modlist(base_plugins + enabled_testable)

        # Read the original backup to preserve order
        source = self.plugins_backup if os.path.exists(self.plugins_backup) else self.plugins_file
        with open(source, "r", encoding='utf-8-sig') as f:
            original_lines = f.readlines()

        with open(self.plugins_file, "w", encoding='utf-8') as f:
            for line in original_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    f.write(line)
                    continue
                # Get plugin name (strip * prefix if present)
                was_enabled = stripped.startswith("*")
                name = stripped[1:] if was_enabled else stripped
                name_lower = name.lower()
                if name_lower in base_set:
                    # Always keep base plugins enabled
                    f.write("*{}\n".format(name))
                elif name_lower in enabled_set:
                    # Testable plugin that's enabled this round
                    f.write("*{}\n".format(name))
                elif was_enabled:
                    # Originally enabled testable plugin, now disabled — keep in file without * prefix
                    f.write("{}\n".format(name))
                else:
                    # Originally disabled — preserve as-is, don't touch it
                    f.write("{}\n".format(name))

    def save_state(self, state):
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        return None

    def has_state(self):
        return os.path.exists(self.state_file)

    def append_log(self, message):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = "[{}] {}".format(ts, message)
        with open(self.log_file, "a") as f:
            f.write(entry + "\n")
        return entry

    def read_log(self):
        if os.path.exists(self.log_file):
            with open(self.log_file, "r") as f:
                return f.read()
        return ""

    def clear_log(self):
        if os.path.exists(self.log_file):
            os.remove(self.log_file)

    # --- Commands ---

    def setup(self, baseline_fps, all_on_fps):
        """Start bisection. User provides FPS values upfront — no test rounds wasted."""
        if self.has_state():
            return None, "Bisection already in progress. Restore first to start over."

        # Read current load order
        all_plugins = self.read_enabled_plugins()

        # Build plugin -> mod mapping (MO2 API preferred, folder scan fallback)
        self.build_plugin_to_mod_map(all_plugins)
        base, testable, excluded_mods = self.classify_plugins(all_plugins)

        # Build dependency groups
        groups, cascade, deps, all_masters, not_found = self.build_dependency_groups(testable)

        self.clear_log()
        self.append_log("=== MO2 Mod Bisect v3 Started ===")
        self.append_log("Profile: {}".format(os.path.basename(self.profile_dir)))
        self.append_log("Total plugins: {} ({} base/excluded, {} testable, {} cascade)".format(
            len(all_plugins), len(base), len(testable) - len(cascade), len(cascade)))
        self.append_log("Baseline: {} FPS | All on: {} FPS | Cost: {} FPS".format(
            baseline_fps, all_on_fps, baseline_fps - all_on_fps))
        self.append_log("{} dependency groups".format(len(groups)))
        if not_found:
            self.append_log("WARNING: {} plugins not found on disk (can't read masters):".format(len(not_found)))
            for p in not_found[:20]:
                self.append_log("  [?] {}".format(p))
            if len(not_found) > 20:
                self.append_log("  ... and {} more".format(len(not_found) - 20))
        if excluded_mods:
            self.append_log("Excluded frameworks ({} mods):".format(len(excluded_mods)))
            for m in sorted(excluded_mods):
                self.append_log("  [skip] {}".format(m))
        if cascade:
            self.append_log("Cascade (always off): {}".format(", ".join(cascade)))
        multi = [g for g in groups if len(g) > 1]
        for g in multi:
            self.append_log("  Group: {} (+{} deps)".format(g[0], len(g) - 1))

        total_cost = baseline_fps - all_on_fps
        if total_cost <= 5:
            msg = "Only {} FPS difference. Nothing significant to find.".format(total_cost)
            self.append_log(msg)
            return None, msg

        # Backup
        self.backup_files()

        # Start bisecting — split into two halves, test first half
        all_indices = list(range(len(groups)))
        half = len(all_indices) // 2
        half_a = all_indices[:half]
        half_b = all_indices[half:]

        work_queue = [
            {"indices": half_b, "label": "B"},
        ]

        first_test = {"indices": half_a, "label": "A"}
        plugins = self.order_plugins([groups[i] for i in half_a], deps)
        # Close under masters — pull in any missing dependencies
        base_set = {p.lower() for p in base}
        plugins, pulled = self.close_under_masters(plugins, testable, all_masters, base_set)
        if pulled:
            self.append_log("Pulled in {} extra plugins for masters: {}".format(
                len(pulled), ", ".join(pulled[:10])))
        self.write_plugins(base, plugins)

        state = {
            "base_plugins": base,
            "all_testable": testable,
            "groups": groups,
            "cascade": cascade,
            "all_deps": deps,
            "all_masters": all_masters,
            "plugin_to_mod": self._plugin_to_mod,
            "phase": "testing",
            "baseline_fps": baseline_fps,
            "all_on_fps": all_on_fps,
            "work_queue": work_queue,
            "culprits": [],
            "enabled_indices": half_a,
            "current_test": first_test,
            "round": 0,
            "history": [],
        }
        self.save_state(state)

        total_testable = sum(len(g) for g in groups)
        msg = "{} testable plugins in {} groups.\nTesting A ({} groups, {} plugins).\nLaunch game and report FPS.".format(
            total_testable, len(groups), len(half_a), len(plugins))
        self.append_log(msg)
        return state, msg

    def report_fps(self, fps):
        state = self.load_state()
        if not state:
            return None, "No bisection in progress."

        groups = state["groups"]
        deps = state["all_deps"]
        base = state["base_plugins"]
        all_testable = state.get("all_testable", [])
        all_masters_map = state.get("all_masters", {})
        base_set = {p.lower() for p in base}
        self._plugin_to_mod = state.get("plugin_to_mod", {})
        phase = state["phase"]

        if phase != "testing":
            return state, "Bisection is not in testing phase."

        baseline = state["baseline_fps"]
        test = state["current_test"]
        indices = test["indices"]
        fps_cost = baseline - fps

        state["round"] += 1
        self.append_log("Round {} — {} = {} FPS (cost: {} FPS, {} groups)".format(
            state["round"], test["label"], fps, fps_cost, len(indices)))

        state["history"].append({
            "round": state["round"],
            "label": test["label"],
            "groups": len(indices),
            "fps": fps,
            "cost": fps_cost,
        })

        if fps_cost > 5 and len(indices) > self.THRESHOLD:
            half = len(indices) // 2
            sub_a = indices[:half]
            sub_b = indices[half:]
            state["work_queue"].append(
                {"indices": sub_a, "label": test["label"] + ".A"})
            state["work_queue"].append(
                {"indices": sub_b, "label": test["label"] + ".B"})
            self.append_log("  Splitting {} further".format(test["label"]))

        elif fps_cost > 5 and len(indices) <= self.THRESHOLD:
            group_names = []
            for i in indices:
                g = groups[i]
                if len(g) == 1:
                    group_names.append(g[0])
                else:
                    group_names.append("{} (+{})".format(g[0], len(g) - 1))
            state["culprits"].append({
                "indices": indices,
                "names": group_names,
                "fps_cost": fps_cost,
            })
            self.append_log("  CULPRIT: {} (costs {} FPS)".format(
                ", ".join(group_names), fps_cost))
        else:
            self.append_log("  Clean (cost <= 5 FPS)")

        # Next task
        if state["work_queue"]:
            next_test = state["work_queue"].pop(0)
            next_indices = next_test["indices"]
            plugins = self.order_plugins([groups[i] for i in next_indices], deps)
            plugins, pulled = self.close_under_masters(plugins, all_testable, all_masters_map, base_set)
            if pulled:
                self.append_log("  Pulled in {} masters: {}".format(
                    len(pulled), ", ".join(pulled[:5])))
            self.write_plugins(base, plugins)
            state["current_test"] = next_test
            state["enabled_indices"] = next_indices
            self.save_state(state)

            msg = "Testing {} ({} groups, {} plugins).\n{} tests remaining.".format(
                next_test["label"], len(next_indices), len(plugins),
                len(state["work_queue"]))
            self.append_log(msg)
            return state, msg
        else:
            self.write_plugins(base, [])
            state["phase"] = "done"
            state["enabled_indices"] = []
            self.save_state(state)

            if state["culprits"]:
                self.append_log("=== RESULTS ===")
                total_found = 0
                for c in sorted(state["culprits"], key=lambda x: x["fps_cost"], reverse=True):
                    self.append_log("  -{} FPS: {}".format(
                        c["fps_cost"], ", ".join(c["names"])))
                    total_found += c["fps_cost"]
                self.append_log("Total: {} FPS of {} FPS total cost".format(
                    total_found, baseline - state["all_on_fps"]))
                msg = "Done! Found {} culprit(s). Check the log.".format(
                    len(state["culprits"]))
            else:
                msg = "Done. No single group costs more than 5 FPS."
                self.append_log(msg)

            return state, msg

    def restore(self):
        self.restore_backups()
        self.append_log("=== Restored original files ===")
        return "Restored. Profile is back to pre-bisection state."

    def get_status_text(self):
        state = self.load_state()
        if not state:
            return "No bisection in progress.\nClick 'Start Bisection' to begin."

        lines = []
        phase = state.get("phase", "?")
        groups = state.get("groups", [])
        total_plugins = sum(len(g) for g in groups)
        lines.append("Plugins: {} base | {} testable in {} groups".format(
            len(state.get("base_plugins", [])), total_plugins, len(groups)))
        lines.append("Round: {}".format(state["round"]))

        if phase == "testing":
            test = state.get("current_test", {})
            n_groups = len(test.get("indices", []))
            lines.append("Phase: Testing '{}'  ({} groups)".format(
                test.get("label", "?"), n_groups))
            lines.append("Baseline: {} FPS | All on: {} FPS".format(
                state.get("baseline_fps", "?"), state.get("all_on_fps", "?")))
            lines.append("Tests remaining: {}".format(len(state.get("work_queue", []))))
            if state.get("culprits"):
                lines.append("Culprits found so far: {}".format(len(state["culprits"])))
        elif phase == "done":
            lines.append("Phase: COMPLETE")
            lines.append("Baseline: {} FPS | All on: {} FPS".format(
                state.get("baseline_fps", "?"), state.get("all_on_fps", "?")))
            if state.get("culprits"):
                lines.append("")
                lines.append("FPS Killers Found:")
                for c in sorted(state["culprits"], key=lambda x: x["fps_cost"], reverse=True):
                    lines.append("  -{} FPS: {}".format(
                        c["fps_cost"], ", ".join(c["names"])))

        if state.get("history"):
            lines.append("")
            lines.append("History:")
            for h in state["history"][-10:]:
                lines.append("  R{}: {} = {} FPS (cost {})".format(
                    h.get("round", "?"), h.get("label", "?"),
                    h.get("fps", "?"), h.get("cost", "?")))

        return "\n".join(lines)


class ModBisectDialog(QDialog):
    def __init__(self, engine, organizer, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.organizer = organizer
        self.setWindowTitle("Mod Bisect v3 - Find FPS Killers")
        self.setMinimumWidth(580)
        self.setMinimumHeight(560)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Status
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setFont(QFont("Consolas", 10))
        self.status_label.setStyleSheet(
            "background: #1e1e2e; color: #cdd6f4; padding: 12px; border-radius: 6px;")
        layout.addWidget(self.status_label)

        # Setup group — baseline FPS inputs
        setup_group = QGroupBox("Start Bisection")
        setup_layout = QHBoxLayout(setup_group)
        setup_layout.addWidget(QLabel("Good FPS:"))
        self.baseline_input = QSpinBox()
        self.baseline_input.setRange(1, 999)
        self.baseline_input.setValue(150)
        self.baseline_input.setToolTip("FPS with no mods / known good state")
        self.baseline_input.setStyleSheet("font-size: 14px; padding: 4px; min-width: 70px;")
        setup_layout.addWidget(self.baseline_input)
        setup_layout.addWidget(QLabel("Bad FPS:"))
        self.allon_input = QSpinBox()
        self.allon_input.setRange(1, 999)
        self.allon_input.setValue(50)
        self.allon_input.setToolTip("FPS with full load order")
        self.allon_input.setStyleSheet("font-size: 14px; padding: 4px; min-width: 70px;")
        setup_layout.addWidget(self.allon_input)
        self.setup_btn = QPushButton("Start")
        self.setup_btn.setStyleSheet("font-size: 13px; padding: 8px;")
        self.setup_btn.clicked.connect(self._on_setup)
        setup_layout.addWidget(self.setup_btn)
        layout.addWidget(setup_group)

        # FPS input + submit
        fps_group = QGroupBox("Report FPS")
        fps_layout = QHBoxLayout(fps_group)
        fps_layout.addWidget(QLabel("FPS:"))
        self.fps_input = QSpinBox()
        self.fps_input.setRange(1, 999)
        self.fps_input.setValue(60)
        self.fps_input.setStyleSheet("font-size: 18px; padding: 8px; min-width: 100px;")
        fps_layout.addWidget(self.fps_input)
        self.submit_btn = QPushButton("Submit FPS")
        self.submit_btn.setStyleSheet(
            "font-size: 15px; font-weight: bold; padding: 12px;"
            "background-color: #1e66f5; color: white; border-radius: 6px;")
        self.submit_btn.clicked.connect(self._on_submit_fps)
        fps_layout.addWidget(self.submit_btn)
        self.crash_btn = QPushButton("Crashed")
        self.crash_btn.setStyleSheet(
            "font-size: 13px; font-weight: bold; padding: 12px;"
            "background-color: #d20f39; color: white; border-radius: 6px;")
        self.crash_btn.clicked.connect(self._on_crash)
        fps_layout.addWidget(self.crash_btn)
        layout.addWidget(fps_group)

        # Restore
        self.restore_btn = QPushButton("Restore Original Files")
        self.restore_btn.setStyleSheet("font-size: 11px; padding: 6px;")
        self.restore_btn.clicked.connect(self._on_restore)
        layout.addWidget(self.restore_btn)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # Log
        log_label = QLabel("Test Log:")
        log_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(log_label)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet(
            "background: #1e1e2e; color: #a6adc8; padding: 6px; border-radius: 4px;")
        layout.addWidget(self.log_text)

        copy_btn = QPushButton("Copy Log to Clipboard")
        copy_btn.setStyleSheet("font-size: 11px; padding: 5px;")
        copy_btn.clicked.connect(self._copy_log)
        layout.addWidget(copy_btn)

    def _refresh(self):
        state = self.engine.load_state()
        has_state = state is not None
        phase = state.get("phase", "") if state else ""
        in_progress = has_state and phase not in ("done", "")

        self.setup_btn.setEnabled(not has_state)
        self.baseline_input.setEnabled(not has_state)
        self.allon_input.setEnabled(not has_state)
        self.submit_btn.setEnabled(in_progress)
        self.fps_input.setEnabled(in_progress)
        self.crash_btn.setEnabled(in_progress)
        self.restore_btn.setEnabled(has_state)

        self.status_label.setText(self.engine.get_status_text())
        self.log_text.setPlainText(self.engine.read_log())
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_setup(self):
        baseline = self.baseline_input.value()
        allon = self.allon_input.value()
        if baseline <= allon:
            QMessageBox.warning(self, "Error",
                "Good FPS ({}) must be higher than Bad FPS ({}).".format(baseline, allon))
            return
        reply = QMessageBox.question(
            self, "Start Bisection",
            "Good FPS: {}  |  Bad FPS: {}\n"
            "FPS cost to find: {} FPS\n\n"
            "This will back up your files and start bisecting\n"
            "your entire load order. Continue?".format(
                baseline, allon, baseline - allon),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        state, msg = self.engine.setup(baseline, allon)
        if state is None:
            QMessageBox.warning(self, "Error", msg)
        else:
            QMessageBox.information(self, "Ready",
                msg + "\n\nLaunch the game, note your FPS, then\n"
                "come back and enter it here.")
            self._try_refresh_mo2()
        self._refresh()

    def _on_submit_fps(self):
        fps = self.fps_input.value()
        state, msg = self.engine.report_fps(fps)
        if state and state.get("phase") == "done":
            QMessageBox.information(self, "Complete", msg)
        else:
            QMessageBox.information(self, "Next Test",
                msg + "\n\nLaunch the game, note your FPS, then enter it here.")
            self._try_refresh_mo2()
        self._refresh()

    def _on_crash(self):
        reply = QMessageBox.question(
            self, "Game Crashed",
            "Retry same test, or skip this group?\n\n"
            "Retry = same plugins, test again\n"
            "Skip = treat as bad FPS, keep splitting",
            QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Ignore,
        )
        state = self.engine.load_state()
        if not state:
            return
        test = state.get("current_test", {})
        self.engine.append_log("CRASHED (testing {})".format(test.get("label", "?")))

        if reply == QMessageBox.StandardButton.Retry:
            self.engine.append_log("  Retrying same test")
            QMessageBox.information(self, "Retry",
                "Same plugins still enabled. Launch game and try again.")
        else:
            state, msg = self.engine.report_fps(0)
            if state and state.get("phase") == "done":
                QMessageBox.information(self, "Complete", msg)
            else:
                QMessageBox.information(self, "Next Test",
                    msg + "\n\nLaunch the game, note your FPS, then enter it here.")
                self._try_refresh_mo2()
        self._refresh()

    def _on_restore(self):
        reply = QMessageBox.question(
            self, "Restore",
            "Restore plugins.txt and modlist.txt to pre-bisection state?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        msg = self.engine.restore()
        self._try_refresh_mo2()
        QMessageBox.information(self, "Restored", msg)
        self._refresh()

    def _try_refresh_mo2(self):
        try:
            self.organizer.refresh()
        except Exception:
            pass

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_text.toPlainText())
        QMessageBox.information(self, "Copied", "Log copied to clipboard.")


class ModBisectPlugin(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self.__organizer = None
        self.__parent = None

    def init(self, organizer):
        self.__organizer = organizer
        return True

    def name(self):
        return "ModBisect"

    def localizedName(self):
        return self.tr("Mod Bisect Tool")

    def author(self):
        return "Claude"

    def description(self):
        return self.tr(
            "Find FPS-killing plugins via binary search. "
            "Bisects your entire load order. "
            "Syncs both left pane (mods) and right pane (plugins).")

    def version(self):
        return mobase.VersionInfo(3, 0, 0, 0)

    def settings(self):
        return [
            mobase.PluginSetting(
                "extra_excludes",
                self.tr("Extra mod name patterns to never disable (comma-separated)"),
                ""),
        ]

    def displayName(self):
        return self.tr("Mod Bisect Tool")

    def tooltip(self):
        return self.tr("Find FPS-killing plugins via binary search")

    def icon(self):
        return QIcon()

    def setParentWidget(self, widget):
        self.__parent = widget

    def display(self):
        org = self.__organizer
        profile_dir = org.profilePath()
        mods_dir = os.path.join(org.basePath(), "mods")

        # Build exclude list
        extra = org.pluginSetting(self.name(), "extra_excludes")
        exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
        if extra:
            for p in extra.split(","):
                p = p.strip()
                if p:
                    exclude_patterns.append(p)

        overwrite_dir = org.overwritePath()
        engine = BisectEngine(profile_dir, mods_dir, overwrite_dir, exclude_patterns, organizer=org)
        dlg = ModBisectDialog(engine, org, self.__parent)
        dlg.exec()

    def tr(self, s):
        return QCoreApplication.translate("ModBisectPlugin", s)


def createPlugin():
    return ModBisectPlugin()
