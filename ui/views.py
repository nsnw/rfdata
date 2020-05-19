from django.shortcuts import render
from django.http import HttpResponse
from data.models import Chat, Mailbox, Command


def main(request):
    return render(
        request,
        'index.html'
    )


def chats(request):
    chats = Chat.objects.all()

    return render(
        request,
        'chats.html',
        {
            'chats': chats
        }
    )


def chat(request, name: str):
    try:
        chat = Chat.objects.get(name=name.upper())

    except Chat.DoesNotExist:
        return HttpResponse(status=404)

    members = chat.members
    messages = chat.messages.order_by('-timestamp')[:100]

    return render(
        request,
        'chat.html',
        {
            'chat_name': chat.name,
            'chat_topic': chat.topic,
            'members': members,
            'messages': messages
        }
    )


def mailboxes(request):
    mailboxes = Mailbox.objects.all()

    return render(
        request,
        'mailboxes.html',
        {
            'mailboxes': mailboxes
        }
    )


def mailbox(request, name: str):
    try:
        mailbox = Mailbox.objects.get(owner__station=name.upper())

    except Mailbox.DoesNotExist:
        return HttpResponse(status=404)

    messages = mailbox.message_set.all()

    return render(
        request,
        'mailbox.html',
        {
            'mailbox': mailbox,
            'messages': messages
        }
    )


def commands(request):
    commands = Command.objects.all().order_by('-pk')[:100]

    return render(
        request,
        'commands.html',
        {
            'commands': commands
        }
    )
