"""Exercise the exact PyPI engine used by the project, with disposable data."""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import relate


def main():
    Path(".papertrail").mkdir(exist_ok=True)
    with TemporaryDirectory(dir=".papertrail") as directory:
        for layout in (":memory:", str(Path(directory, "folder"))):
            with relate.connect(layout) as db:
                assert db.execute("RETURN 1 AS ok").data() == [{"ok": 1}]
        print("memory and directory stores opened")
        path = str(Path(directory, "probe.rl"))
        with relate.connect(path) as db:
            db.execute("CREATE (:H {id: 1})-[:R]->(:H {id: 2})-[:R]->(:H {id: 3})")
            for query in [
                "MATCH p=(h:H {id:$id})-[:R*0..]->(a) RETURN a.id AS id, length(p) AS hops",
                "MATCH p=(:H {id:$id})-[:R*2]->(a) RETURN p",
                "MATCH (h:H) WHERE EXISTS { MATCH (h)-[:R]->() } RETURN h.id AS id",
                "CREATE INDEX h_idx FOR (h:H) ON (h.id)",
                "DROP INDEX h_idx",
                "CREATE CONSTRAINT h_unique FOR (h:H) REQUIRE h.id IS UNIQUE",
            ]:
                print(query, db.execute(query, {"id": 1}).data())
                if query.startswith("CREATE INDEX"):
                    try:
                        db.execute("CREATE CONSTRAINT unique_demo FOR (h:H) REQUIRE h.id IS UNIQUE")
                    except relate.Error as exc:
                        print("index on same property blocks constraint", type(exc).__name__)
                    else:
                        raise AssertionError("Expected an index conflict")
            db.autocommit = False
            db.execute("CREATE (:Undo)")
            db.rollback()
            assert db.execute("MATCH (n:Undo) RETURN count(n) AS n").data() == [{"n": 0}]
            print("rollback removed uncommitted node")
            for method in ("executemany", "UNWIND"):
                rows = [{"id": i} for i in range(1000)]
                start = perf_counter()
                if method == "executemany":
                    db.executemany("CREATE (:Sample {id:$id})", rows)
                else:
                    db.execute("UNWIND $rows AS row CREATE (n:Sample) SET n = row", {"rows": rows})
                db.commit()
                print(
                    method, "1000 nodes incl. commit", round(perf_counter() - start, 4), "seconds"
                )
        with relate.connect(path, read_only=True) as db:
            print("read_only", db.execute("MATCH (s:Sample) RETURN count(s) AS n").data())
            try:
                db.execute("CREATE (:Forbidden)")
            except relate.Error as exc:
                print("write rejected", type(exc).__name__)
            else:
                raise AssertionError("Expected read-only store")


if __name__ == "__main__":
    main()
