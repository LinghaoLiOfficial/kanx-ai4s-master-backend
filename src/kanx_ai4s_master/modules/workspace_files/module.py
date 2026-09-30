from ...core.modules import MigrationDescriptor, ModuleSpec
from .models import WorkspaceFile
from .router import router

module = ModuleSpec(
    name="workspace_files",
    requires=("auth", "rbac"),
    routers=(router,),
    models=(WorkspaceFile,),
    migrations=(MigrationDescriptor("workspace_files"),),
    permissions=("workspace_files:read", "workspace_files:write", "workspace_files:delete"),
)
