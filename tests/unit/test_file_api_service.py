"""
File RESTful service 层单元测试 —— api/apps/services/file_api_service.py
对标 ragflow #13741（files /file API to RESTful）收编后的 multirag 实现。
共享 fixtures（db）见 tests/unit/conftest.py。

被测对象是 service 层纯编排函数，DB / 存储访问全部通过 monkeypatch 打桩。
另含 restful_apis/file_api.py 里 MoveFileReq 的 Pydantic 校验器测试（ragflow mv 语义守卫）。
"""

import types

import pytest

import api.apps.services.file_api_service as svc


def _file(**kw):
    """构造一个 File 风格的假对象（带 to_dict）。"""
    defaults = {
        "id": "f1",
        "parent_id": "pf1",
        "tenant_id": "t1",
        "created_by": "t1",
        "name": "old.txt",
        "location": "old.txt",
        "size": 10,
        "type": "1",
        "source_type": None,
    }
    defaults.update(kw)
    obj = types.SimpleNamespace(**defaults)
    obj.to_dict = lambda _d=dict(defaults): dict(_d)
    return obj


# ============================ create_folder ============================


def test_create_folder_parent_missing(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "is_parent_folder_exist", lambda d, pid: False)
    ok, msg = svc.create_folder(db, "t1", "docs", pf_id="pf1")
    assert ok is False
    assert "Parent Folder Doesn't Exist" in msg


def test_create_folder_duplicate(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "is_parent_folder_exist", lambda d, pid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [_file(name="docs")])
    ok, msg = svc.create_folder(db, "t1", "docs", pf_id="pf1")
    assert ok is False
    assert "Duplicated folder name" in msg


def test_create_folder_success_virtual(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "is_parent_folder_exist", lambda d, pid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [])
    captured = {}

    def _insert(d, data):
        captured.update(data)
        return _file(**data)

    monkeypatch.setattr(svc.FileService, "insert", _insert)
    # type 非 FOLDER -> VIRTUAL
    ok, data = svc.create_folder(db, "t1", "note", pf_id="pf1", file_type="something")
    assert ok is True
    assert captured["type"] == svc.FileType.VIRTUAL.value
    assert data["name"] == "note"


def test_create_folder_success_folder_type(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "is_parent_folder_exist", lambda d, pid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [])
    captured = {}
    monkeypatch.setattr(svc.FileService, "insert", lambda d, data: (captured.update(data), _file(**data))[1])
    ok, _ = svc.create_folder(db, "t1", "docs", pf_id="pf1", file_type=svc.FileType.FOLDER.value)
    assert ok is True
    assert captured["type"] == svc.FileType.FOLDER.value


@pytest.mark.parametrize("file_type", ["FOLDER", "Folder"])
def test_create_folder_accepts_folder_type_case_insensitively(monkeypatch, db, file_type):
    """调用方按枚举名大写传 FOLDER 时，不能悄悄建成 VIRTUAL 文件。"""
    monkeypatch.setattr(svc.FileService, "is_parent_folder_exist", lambda d, pid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [])
    captured = {}
    monkeypatch.setattr(svc.FileService, "insert", lambda d, data: (captured.update(data), _file(**data))[1])
    ok, _ = svc.create_folder(db, "t1", "docs", pf_id="pf1", file_type=file_type)
    assert ok is True
    assert captured["type"] == svc.FileType.FOLDER.value


def test_create_folder_without_file_type_stays_virtual(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "is_parent_folder_exist", lambda d, pid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [])
    captured = {}
    monkeypatch.setattr(svc.FileService, "insert", lambda d, data: (captured.update(data), _file(**data))[1])
    ok, _ = svc.create_folder(db, "t1", "note", pf_id="pf1")
    assert ok is True
    assert captured["type"] == svc.FileType.VIRTUAL.value


# ============================ list_files ============================


def test_list_files_folder_not_found(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, pid: None)
    ok, msg = svc.list_files(db, "t1", {"parent_id": "pf1"})
    assert ok is False
    assert "Folder not found" in msg


def test_list_files_success(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, pid: _file(id=pid))
    monkeypatch.setattr(svc.FileService, "get_by_pf_id", lambda d, t, pid, p, ps, ob, ds, kw: ([{"id": "a"}], 1))
    monkeypatch.setattr(svc.FileService, "get_parent_folder", lambda d, pid: _file(id="parent"))
    ok, data = svc.list_files(db, "t1", {"parent_id": "pf1", "page": 1, "page_size": 15})
    assert ok is True
    assert data["total"] == 1
    assert data["files"] == [{"id": "a"}]
    assert data["parent_folder"]["id"] == "parent"


# ============ get_parent_folder / get_all_parent_folders（IDOR 钉板） ============


def test_get_parent_folder_no_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: _file())
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: False)
    ok, msg = svc.get_parent_folder(db, "f1", user_id="intruder")
    assert ok is False
    assert msg == "No authorization."


def test_get_parent_folder_success_with_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: _file())
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: True)
    monkeypatch.setattr(svc.FileService, "get_parent_folder", lambda d, fid: _file(id="pf1", name="parent"))
    ok, result = svc.get_parent_folder(db, "f1", user_id="u1")
    assert ok is True
    assert result["parent_folder"]["id"] == "pf1"


def test_get_all_parent_folders_no_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: _file())
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: False)
    ok, msg = svc.get_all_parent_folders(db, "f1", user_id="intruder")
    assert ok is False
    assert msg == "No authorization."


def test_get_all_parent_folders_success_with_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: _file())
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: True)
    monkeypatch.setattr(svc.FileService, "get_all_parent_folders", lambda d, fid: [_file(id="pf1"), _file(id="root")])
    ok, result = svc.get_all_parent_folders(db, "f1", user_id="u1")
    assert ok is True
    assert [f["id"] for f in result["parent_folders"]] == ["pf1", "root"]


# ============================ delete_files ============================


def test_delete_file_not_found(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: None)
    ok, msg = svc.delete_files(db, "t1", ["f1"])
    assert ok is False
    assert "File or Folder not found" in msg


def test_delete_no_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: _file())
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: False)
    ok, msg = svc.delete_files(db, "t1", ["f1"])
    assert ok is False
    assert "No authorization" in msg


def test_delete_knowledgebase_source_skipped(monkeypatch, db):
    """source_type 为 KNOWLEDGEBASE 的文件被跳过，不触发存储/文档删除。"""
    kb_file = _file(source_type=svc.FileSource.KNOWLEDGEBASE)
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: kb_file)
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: True)
    deleted = []
    monkeypatch.setattr(svc.FileService, "delete", lambda d, f: deleted.append(f))
    ok, res = svc.delete_files(db, "t1", ["f1"])
    assert ok is True and res is True
    assert deleted == []  # 跳过，未删除


def test_delete_single_file_success(monkeypatch, db):
    f = _file(type="1", location="old.txt", source_type=None)
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: f)
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, fo, uid: True)
    rm_calls, del_calls = [], []
    monkeypatch.setattr(svc.settings, "STORAGE_IMPL", types.SimpleNamespace(rm=lambda b, n: rm_calls.append((b, n))))
    monkeypatch.setattr(svc.File2DocumentService, "get_by_file_id", lambda d, fid: [])
    monkeypatch.setattr(svc.FileService, "delete", lambda d, fo: del_calls.append(fo))
    ok, res = svc.delete_files(db, "t1", ["f1"])
    assert ok is True and res is True
    assert rm_calls == [("pf1", "old.txt")]
    assert del_calls == [f]


# ============================ move_files (mv 语义) ============================


def test_move_source_not_found(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_ids", lambda d, ids: [])
    ok, msg = svc.move_files(db, "t1", ["f1"], dest_file_id="dst")
    assert ok is False
    assert "Source files not found" in msg


def test_move_no_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_ids", lambda d, ids: [_file()])
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: False)
    ok, msg = svc.move_files(db, "t1", ["f1"], dest_file_id="dst")
    assert ok is False
    assert "No authorization" in msg


def test_move_rename_extension_change_rejected(monkeypatch, db):
    f = _file(name="old.txt", type="1")
    monkeypatch.setattr(svc.FileService, "get_by_ids", lambda d, ids: [f])
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, fo, uid: True)
    ok, msg = svc.move_files(db, "t1", ["f1"], new_name="new.pdf")
    assert ok is False
    assert "extension of file can't be changed" in msg


def test_move_rename_duplicate_name(monkeypatch, db):
    f = _file(name="old.txt", type="1", parent_id="pf1")
    monkeypatch.setattr(svc.FileService, "get_by_ids", lambda d, ids: [f])
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, fo, uid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [_file(name="new.txt")])
    ok, msg = svc.move_files(db, "t1", ["f1"], new_name="new.txt")
    assert ok is False
    assert "Duplicated file name" in msg


def test_move_pure_rename_syncs_document_name(monkeypatch, db):
    """纯重命名（无 dest）：更新文件名并同步关联文档名（ragflow 行为增强）。"""
    f = _file(id="f1", name="old.txt", type="1", parent_id="pf1")
    monkeypatch.setattr(svc.FileService, "get_by_ids", lambda d, ids: [f])
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, fo, uid: True)
    monkeypatch.setattr(svc.FileService, "query", lambda d, **kw: [])
    file_updates, doc_updates = [], []
    monkeypatch.setattr(svc.FileService, "update_by_id", lambda d, fid, upd: (file_updates.append((fid, upd)), True)[1])
    monkeypatch.setattr(svc.File2DocumentService, "get_by_file_id", lambda d, fid: [types.SimpleNamespace(document_id="doc1")])
    monkeypatch.setattr(svc.DocumentService, "update_by_id", lambda d, did, upd: (doc_updates.append((did, upd)), True)[1])

    ok, res = svc.move_files(db, "t1", ["f1"], new_name="new.txt")
    assert ok is True and res is True
    assert file_updates == [("f1", {"name": "new.txt"})]
    assert doc_updates == [("doc1", {"name": "new.txt"})]


def test_move_to_folder_triggers_storage_move(monkeypatch, db):
    """移动到新文件夹（无改名）：跨父目录时调用存储层 move 并更新 parent_id/location。"""
    f = _file(id="f1", name="a.txt", type="1", parent_id="pf1", location="a.txt")
    dest = _file(id="pf2", type=svc.FileType.FOLDER.value)
    monkeypatch.setattr(svc.FileService, "get_by_ids", lambda d, ids: [f])
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, fo, uid: True)
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: dest)
    moved, updated = [], []
    monkeypatch.setattr(
        svc.settings,
        "STORAGE_IMPL",
        types.SimpleNamespace(
            obj_exist=lambda b, n: False,
            move=lambda sp, sl, dp, dl: moved.append((sp, sl, dp, dl)),
        ),
    )
    monkeypatch.setattr(svc.FileService, "update_by_id", lambda d, fid, upd: (updated.append((fid, upd)), True)[1])

    ok, res = svc.move_files(db, "t1", ["f1"], dest_file_id="pf2")
    assert ok is True and res is True
    assert moved == [("pf1", "a.txt", "pf2", "a.txt")]
    assert updated[0][1]["parent_id"] == "pf2"
    assert updated[0][1]["location"] == "a.txt"


# ============================ get_file_content ============================


def test_get_file_content_not_found(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: None)
    ok, msg = svc.get_file_content(db, "t1", "f1")
    assert ok is False
    assert "Document not found" in msg


def test_get_file_content_no_permission(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: _file())
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, f, uid: False)
    ok, msg = svc.get_file_content(db, "t1", "f1")
    assert ok is False
    assert "No authorization" in msg


def test_get_file_content_success(monkeypatch, db):
    f = _file()
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, fid: f)
    monkeypatch.setattr(svc, "check_file_team_permission", lambda d, fo, uid: True)
    ok, res = svc.get_file_content(db, "t1", "f1")
    assert ok is True
    assert res is f


# ============================ upload_file 守卫 ============================


def test_upload_folder_not_found(monkeypatch, db):
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, pid: None)
    ok, msg = svc.upload_file(db, "t1", "pf1", [(b"x", "a.txt")])
    assert ok is False
    assert "Can't find this folder" in msg


def test_upload_exceeds_max_file_num(monkeypatch, db):
    monkeypatch.setenv("MAX_FILE_NUM_PER_USER", "1")
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda d, pid: _file(id=pid))
    monkeypatch.setattr(svc.DocumentService, "get_doc_count", lambda d, uid: 5)
    ok, msg = svc.upload_file(db, "t1", "pf1", [(b"x", "a.txt")])
    assert ok is False
    assert "maximum file number" in msg


# ============================ MoveFileReq 校验器 ============================


def test_movereq_requires_dest_or_name():
    from pydantic import ValidationError

    from api.apps.restful_apis.file_api import MoveFileReq

    with pytest.raises(ValidationError):
        MoveFileReq(src_file_ids=["f1"])  # dest 和 new_name 都缺


def test_movereq_new_name_single_file_only():
    from pydantic import ValidationError

    from api.apps.restful_apis.file_api import MoveFileReq

    with pytest.raises(ValidationError):
        MoveFileReq(src_file_ids=["f1", "f2"], new_name="x.txt")


def test_movereq_valid_combinations():
    from api.apps.restful_apis.file_api import MoveFileReq

    assert MoveFileReq(src_file_ids=["f1"], dest_file_id="d").new_name is None
    assert MoveFileReq(src_file_ids=["f1"], new_name="x.txt").dest_file_id is None
    both = MoveFileReq(src_file_ids=["f1"], dest_file_id="d", new_name="x.txt")
    assert both.dest_file_id == "d" and both.new_name == "x.txt"
