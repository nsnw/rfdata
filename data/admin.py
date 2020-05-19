from django.contrib import admin
from data.models import Chat, ChatMessage, ChatSubscription, Station, Message, Mailbox

# Register your models here.
admin.site.register(Chat)
admin.site.register(ChatMessage)
admin.site.register(ChatSubscription)
admin.site.register(Station)
admin.site.register(Message)
admin.site.register(Mailbox)
