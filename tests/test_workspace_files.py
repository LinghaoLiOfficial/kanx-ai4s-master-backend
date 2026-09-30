from kanx_ai4s_master.modules.workspace_files.router import _default_mindmap


def test_default_mindmap_uses_file_name_as_root_topic() -> None:
    document = _default_mindmap("产品路线图")

    assert document["tree"]["nodes"]["root"]["text"] == "产品路线图"
