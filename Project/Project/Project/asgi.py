# asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

# Ensure Django settings are properly loaded first
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Project.settings')

from gamedevelopment import routing  # Import routing after settings

application = ProtocolTypeRouter({
    "http": get_asgi_application(),  # Handles HTTP requests
    "websocket": AuthMiddlewareStack(
        URLRouter(routing.websocket_urlpatterns)  # Handles WebSocket requests
    ),
})
