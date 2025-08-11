from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.functions import func

from api.db.db_models import MCPServer
from api.db.services.common_service import CommonService


class MCPServerService(CommonService):
    """Service class for managing MCP server related database operations.

    This class extends CommonService to provide specialized functionality for MCP server management,
    including MCP server creation, updates, and deletions.

    Attributes:
        model: The MCPServer model class for database operations.
    """

    model = MCPServer

    @classmethod
    def get_servers(cls, db: Session, tenant_id: str, id_list: list[str] | None, page_number, items_per_page, orderby, desc, keywords):
        """Retrieve all MCP servers associated with a tenant.

        This method fetches all MCP servers for a given tenant, ordered by creation time.
        It only includes fields for list display.

        Args:
            db (Session): Database session object.
            tenant_id (str): The unique identifier of the tenant.
            id_list (list[str]): Get servers by ID list. Will ignore this condition if None.

        Returns:
            list[dict]: List of MCP server dictionaries containing MCP server details.
                       Returns None if no MCP servers are found.
        """
        fields = [
            cls.model.id,
            cls.model.name,
            cls.model.server_type,
            cls.model.url,
            cls.model.description,
            cls.model.variables,
            cls.model.create_date,
            cls.model.update_date,
        ]

        query = db.query(*fields).filter(cls.model.tenant_id == tenant_id).order_by(cls.model.create_time.desc())

        if id_list:
            query = query.filter(cls.model.id.in_(id_list))
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))
        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())
        if page_number and items_per_page:
            offset = (page_number - 1) * items_per_page
            query = query.offset(offset).limit(items_per_page)

        servers = query.all()
        if not servers:
            return None
        
        server_list = []
        for server in servers:
            server_dict = {
                "id": server.id,
                "name": server.name,
                "server_type": server.server_type,
                "url": server.url,
                "description": server.description,
                "variables": server.variables,
                "update_date": server.update_date
            }
            server_list.append(server_dict)
        
        return server_list


    @classmethod
    def get_by_name_and_tenant(cls, db: Session, name: str, tenant_id: str):
        try:
            mcp_server = db.query(cls.model).filter_by(
                name=name,
                tenant_id=tenant_id
            ).first()
            return bool(mcp_server), mcp_server
        except SQLAlchemyError:
            return False, None