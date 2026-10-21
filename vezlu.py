"""Local text alternatives with immutable snapshots and guarded combination."""

from contextlib import contextmanager
import argparse
import difflib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid


class Error(ValueError):
    """A rejected operation; the existing branch remains unchanged."""


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise Error("name must be 1-64 letters, digits, dots, underscores or hyphens; start with a letter or digit")
    return value


def key(value):
    if not isinstance(value, str) or len(value) > 200 or not value:
        raise Error("artifact key must contain 1-200 characters")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in value.split("/")):
        raise Error("artifact key must use simple relative segments, without traversal")
    return value


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise Error("revision must be a 32-character lowercase hexadecimal token")
    return value


def snapshot_size(snapshot):
    if len(snapshot) > 128 or sum(len(text.encode("utf-8")) for text in snapshot.values()) > 1048576:
        raise Error("snapshot exceeds 128 artifacts or 1 MiB of text")


class Store:
    """A private SQLite store. Artifact keys never become filesystem paths."""

    def __init__(self, directory):
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(directory))))
        current = Path(self.path.anchor)
        for part in self.path.parts[1:]:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise Error("store path must not contain a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise Error("store path must be a directory")
        self.database = self.path / "store.sqlite3"
        self._safe()
        try:
            fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._db() as db:
            for query in [
                "CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS revisions (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, project TEXT NOT NULL REFERENCES projects(name), parent1 TEXT REFERENCES revisions(id), parent2 TEXT REFERENCES revisions(id), source TEXT REFERENCES revisions(id), snapshot TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS branches (project TEXT NOT NULL REFERENCES projects(name), name TEXT NOT NULL, head TEXT NOT NULL REFERENCES revisions(id), PRIMARY KEY(project,name))",
                "CREATE TRIGGER IF NOT EXISTS revisions_no_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT, 'immutable revision'); END",
                "CREATE TRIGGER IF NOT EXISTS revisions_no_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT, 'immutable revision'); END",
            ]:
                db.execute(query)

    def _safe(self):
        for path in [self.path, *self.path.parents]:
            if path.is_symlink():
                raise Error("store path must not contain a symlink")
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Error("store directory must be private: owned by you with mode 0700")
        for suffix in ["", "-journal", "-wal", "-shm"]:
            path = Path(str(self.database) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise Error("database and sidecars must not be a symlink")
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Error("database and sidecars must be private regular files with mode 0600")

    @contextmanager
    def _db(self):
        self._safe()
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _head(db, project, branch):
        name(project)
        name(branch)
        row = db.execute("SELECT head FROM branches WHERE project=? AND name=?", (project, branch)).fetchone()
        if row is None:
            raise Error("project or branch does not exist")
        return row["head"]

    @staticmethod
    def _guard(actual, expected):
        token(expected)
        if actual != expected:
            raise Error(f"stale head: expected {expected}, current {actual}; read and reconsider")

    @staticmethod
    def _snapshot(db, revision):
        return json.loads(db.execute("SELECT snapshot FROM revisions WHERE id=?", (revision,)).fetchone()[0])

    @staticmethod
    def _revision(db, project, snapshot, parent1=None, parent2=None, source=None):
        snapshot_size(snapshot)
        revision = uuid.uuid4().hex
        db.execute("INSERT INTO revisions(id,project,parent1,parent2,source,snapshot) VALUES(?,?,?,?,?,?)",
                   (revision, project, parent1, parent2, source, json.dumps(snapshot, sort_keys=True, ensure_ascii=False)))
        return revision

    @staticmethod
    def _ancestors(db, revision):
        seen = set()
        pending = [revision]
        while pending:
            item = pending.pop()
            if item in seen:
                continue
            seen.add(item)
            row = db.execute("SELECT parent1,parent2 FROM revisions WHERE id=?", (item,)).fetchone()
            if row is None:
                raise Error("broken revision ancestry")
            pending.extend(parent for parent in row if parent is not None)
        return seen

    def create(self, project: str) -> dict:
        name(project)
        with self._db() as db:
            if db.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone():
                raise Error("project already exists")
            db.execute("INSERT INTO projects VALUES(?)", (project,))
            head = self._revision(db, project, {})
            db.execute("INSERT INTO branches VALUES(?,?,?)", (project, "main", head))
            return {"project": project, "branch": "main", "head": head}

    def projects(self) -> list:
        with self._db() as db:
            return [row[0] for row in db.execute("SELECT name FROM projects ORDER BY name")]

    def branches(self, project: str) -> list:
        name(project)
        with self._db() as db:
            rows = db.execute("SELECT name,head FROM branches WHERE project=? ORDER BY name", (project,)).fetchall()
            if not rows:
                raise Error("project does not exist")
            return [dict(row) for row in rows]

    def branch(self, project: str, branch: str, source: str, expected_head: str) -> dict:
        name(branch)
        with self._db() as db:
            head = self._head(db, project, source)
            self._guard(head, expected_head)
            if db.execute("SELECT 1 FROM branches WHERE project=? AND name=?", (project, branch)).fetchone():
                raise Error("branch already exists")
            db.execute("INSERT INTO branches VALUES(?,?,?)", (project, branch, head))
            return {"project": project, "branch": branch, "head": head}

    def read(self, project: str, branch: str, revision: str | None = None) -> dict:
        with self._db() as db:
            head = self._head(db, project, branch)
            if revision is None:
                revision = head
            token(revision)
            if revision not in self._ancestors(db, head):
                raise Error("revision is not in this branch's ancestry")
            return {"head": head, "revision": revision, "artifacts": self._snapshot(db, revision)}

    def write(self, project: str, branch: str, changes: dict[str, str | None], expected_head: str) -> dict:
        if not isinstance(changes, dict) or not 1 <= len(changes) <= 128:
            raise Error("changes must be an object with 1-128 artifact keys")
        for path, text in changes.items():
            key(path)
            if text is not None:
                if not isinstance(text, str):
                    raise Error("artifact content must be text or null for deletion")
                try:
                    size = len(text.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise Error("artifact text must be valid UTF-8") from exc
                if size > 65536:
                    raise Error("artifact text exceeds 64 KiB")
        with self._db() as db:
            head = self._head(db, project, branch)
            self._guard(head, expected_head)
            snapshot = self._snapshot(db, head)
            for path, text in changes.items():
                if text is None:
                    if path not in snapshot:
                        raise Error("cannot delete an artifact that does not exist")
                    del snapshot[path]
                else:
                    snapshot[path] = text
            new = self._revision(db, project, snapshot, head)
            db.execute("UPDATE branches SET head=? WHERE project=? AND name=?", (new, project, branch))
            return {"head": new}

    def history(self, project: str, branch: str, limit: int = 50) -> list:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise Error("history limit must be an integer from 1 to 200")
        with self._db() as db:
            reachable = self._ancestors(db, self._head(db, project, branch))
            rows = db.execute("SELECT id,parent1,parent2,source FROM revisions WHERE project=? ORDER BY seq DESC", (project,))
            result = []
            for row in rows:
                if row["id"] in reachable:
                    result.append(dict(row))
                    if len(result) == limit:
                        break
            return result

    def compare(self, project: str, left: str, right: str) -> dict:
        with self._db() as db:
            a, b = self._head(db, project, left), self._head(db, project, right)
            before, after = self._snapshot(db, a), self._snapshot(db, b)
            changes = {}
            for path in sorted(before.keys() | after.keys()):
                if before.get(path) != after.get(path):
                    diff = "".join(difflib.unified_diff(before.get(path, "").splitlines(True),
                                                       after.get(path, "").splitlines(True),
                                                       fromfile=f"{left}/{path}", tofile=f"{right}/{path}"))
                    changes[path] = {"before": before.get(path), "after": after.get(path), "diff": diff}
            return {"left_head": a, "right_head": b, "changes": changes}

    def merge(self, project: str, source: str, target: str, paths: list[str], expected_source: str, expected_target: str) -> dict:
        if not isinstance(paths, list) or not 1 <= len(paths) <= 128:
            raise Error("paths must be a list of 1-128 artifact keys")
        for path in paths:
            key(path)
        if len(set(paths)) != len(paths):
            raise Error("paths must not contain duplicates")
        if source == target:
            raise Error("source and target branches must differ")
        with self._db() as db:
            s, t = self._head(db, project, source), self._head(db, project, target)
            self._guard(s, expected_source)
            self._guard(t, expected_target)
            common = self._ancestors(db, s) & self._ancestors(db, t)
            if not common:
                raise Error("unrelated revision histories")
            obsolete = set()
            for revision in common:
                obsolete.update(self._ancestors(db, revision) - {revision})
            best = common - obsolete
            if len(best) != 1:
                raise Error("ambiguous common ancestry; choose changes manually")
            base = best.pop()
            b, src, dst = (self._snapshot(db, revision) for revision in (base, s, t))
            result, conflicts = dict(dst), {}
            for path in sorted(paths):
                if path not in b.keys() | src.keys() | dst.keys():
                    raise Error("selected artifact does not exist in base, source or target")
                old, left, right = b.get(path), src.get(path), dst.get(path)
                if left == old or left == right:
                    continue
                if right != old:
                    conflicts[path] = {"base": old, "source": left, "target": right}
                elif left is None:
                    result.pop(path, None)
                else:
                    result[path] = left
            if conflicts:
                return {"ok": False, "head": t, "base": base, "conflicts": conflicts}
            # Partial combinations must not hide source changes still waiting to be selected.
            absorbed = all(src.get(path) == b.get(path) or result.get(path) == src.get(path)
                           for path in b.keys() | src.keys())
            new = self._revision(db, project, result, t, s if absorbed else None, s)
            db.execute("UPDATE branches SET head=? WHERE project=? AND name=?", (new, project, target))
            return {"ok": True, "head": new, "base": base, "conflicts": {}}


OPERATIONS = ("projects", "create", "branches", "branch", "read", "write", "history", "compare", "merge")
DESCRIPTIONS = {
    "projects": "List saved project names.",
    "create": "Create a project with an empty main branch. Returns its head token.",
    "branches": "List a project's branch names and current head tokens.",
    "branch": "Fork source into a new branch, guarded by the source's expected_head.",
    "read": "Read all artifacts and head, or an immutable revision in this branch's ancestry.",
    "write": "Atomically replace whole text artifacts; null deletes. Requires the current expected_head.",
    "history": "List newest reachable revisions with ancestry and merge source references.",
    "compare": "Show exact before/after text and unified differences between two branch heads.",
    "merge": "Combine selected artifact paths using common ancestry. Requires both current heads. Conflicts leave target unchanged.",
}


def serve(store):
    """Expose only the local store operations over standard input/output."""
    from functools import wraps
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    server = MCPServer("Vezlu")

    def register(operation):
        method = getattr(store, operation)

        @wraps(method)
        def tool(**arguments):
            try:
                return json.dumps(method(**arguments), ensure_ascii=False)
            except (Error, sqlite3.Error, OSError) as exc:
                raise ToolError(str(exc)) from exc

        # The wrapped method supplies the input schema; the wire result is JSON text.
        tool.__annotations__ = {**method.__annotations__, "return": str}
        server.tool(name=operation, description=DESCRIPTIONS[operation], structured_output=False)(tool)

    for operation in OPERATIONS:
        register(operation)
    server.run(transport="stdio")


def demo(store):
    """Combine two launch-document fixture alternatives into a third result."""
    project = "launch-demo"
    head = store.create(project)["head"]
    base = store.write(project, "main", {
        "brief.md": "# Launch\nOffer a shared calendar for neighbourhood events.\n",
        "channels.md": "# Channels\nPublish a short announcement.\n",
    }, head)["head"]
    for branch in ["community", "partners", "combined"]:
        store.branch(project, branch, "main", base)
    a = store.write(project, "community", {"brief.md":
        "# Community launch\nInvite 30 neighbours to a free Saturday trial.\nMeasure repeat attendance before expanding.\n"}, base)["head"]
    b = store.write(project, "partners", {"channels.md":
        "# Partner launch\nAsk the library and two local clubs to co-host.\nUse their newsletters and a printable event card.\n"}, base)["head"]
    first = store.merge(project, "community", "combined", ["brief.md"], a, base)
    second = store.merge(project, "partners", "combined", ["channels.md"], b, first["head"])
    if not first["ok"] or not second["ok"]:
        raise Error("demo combination failed")
    return {"text_source": "fixtures", "project": project,
            "alternative_a": store.read(project, "community")["artifacts"],
            "alternative_b": store.read(project, "partners")["artifacts"],
            "comparison": store.compare(project, "community", "partners"),
            "combined": store.read(project, "combined")["artifacts"], "head": second["head"]}


def main():
    parser = argparse.ArgumentParser(description="Keep, compare and combine local text alternatives.")
    parser.add_argument("--store", default="~/.local/share/vezlu", help="private data directory (default: %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve MCP over stdio")
    commands.add_parser("demo", help="save and combine two launch-document fixtures")
    call = commands.add_parser("call", help="call a store operation with a JSON object")
    call.add_argument("operation", help=", ".join(OPERATIONS))
    call.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()
    try:
        store = Store(args.store)
        if args.command == "serve":
            serve(store)
            return 0
        if args.command == "demo":
            result = demo(store)
        else:
            if args.operation not in OPERATIONS:
                raise Error("unknown operation; choose " + ", ".join(OPERATIONS))
            if len(args.arguments.encode("utf-8")) > 2097152:
                raise Error("JSON arguments exceed 2 MiB")
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise Error("arguments must be a JSON object")
            result = getattr(store, args.operation)(**arguments)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Error, TypeError, json.JSONDecodeError, UnicodeError, sqlite3.Error, OSError) as exc:
        output = sys.stderr if args.command == "serve" else sys.stdout
        print(json.dumps({"error": str(exc)}), file=output)
        return 1


if __name__ == "__main__":
    sys.exit(main())
