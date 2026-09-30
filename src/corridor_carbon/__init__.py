"""零碳运输走廊碳核算与核验领域服务。

在 ``transport_coordination`` 提供的组织、角色、幂等、审计链与 SQLite
事务边界之上，实现路段边界等主数据版本化、核算批次冻结、证据补证、
绿证占用防重复计算、独立核验发布以及重述/吊销版本管理。
"""

from .service import CarbonService

__all__ = ["CarbonService"]
