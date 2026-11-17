"""Explicit publication helpers for synthetic retrieval fixtures."""

from research_library.build import project_text


def publish_all(root):
    current = project_text.editorial_store(root).current()
    for source in current.snapshots["core"]._bundle["sources"]:
        project_text.publish(root, snapshot_id=current.release_id, source_id=source["id"],
            reviewer="Synthetic fixture admin", reason="Approve fictional fixture text for retrieval", checked=True)
