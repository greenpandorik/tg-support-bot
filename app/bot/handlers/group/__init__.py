from . import command
from . import callback_query
from . import message

routers = [
    command.router_id,
    command.router,
    message.router,
    callback_query.router,
]
