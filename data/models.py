from django.db import models
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)-12s/%(funcName)-16s\n-> %(message)s'
)
logger = logging.getLogger(__name__)


class Station(models.Model):
    station = models.CharField(max_length=16, null=False, blank=False)
    last_read = models.DateTimeField(null=True, blank=True)
    pounce = models.BooleanField(default=False)

    @property
    def mailbox(self):
        mailbox, created = Mailbox.objects.get_or_create(owner=self)

        if created:
            logger.info("Creating mailbox for {}".format(self))
            mailbox.save()

        return mailbox

    def __str__(self):
        return self.station

    def __repr__(self):
        return "<Station: {}>".format(self.station)


class Mailbox(models.Model):
    owner = models.ForeignKey(Station, null=False, blank=False, on_delete=models.CASCADE)
    unread = models.BooleanField(default=False)

    @property
    def messages(self):
        return self.message_set.filter(deleted=False)

    @property
    def count(self):
        return self.messages.count()

    def add(self, source, message):
        message = Message(mailbox=self, source=source, message=message)
        message.save()

        logger.info("Created message from {} to {}: {}".format(
            source.station, self.owner.station, message
        ))

        return message

    def get_message(self):
        message = self.messages.order_by('timestamp').first()

        return message

    def get_message_by_number(self, number):
        try:
            message = self.messages[number-1]
            return message

        except IndexError:
            return False

    def delete_message_by_number(self, number):
        try:
            message = self.messages[number-1]
            message.deleted = True
            message.save()

            return True

        except IndexError:
            return False

    def clear(self):
        logger.info("Clearing mailbox for {}".format(self.owner.station))
        self.message_set.all().delete()

    def __str__(self):
        return self.owner.station

    def __repr__(self):
        return "<Mailbox: {}>".format(self.owner.station)


class Message(models.Model):
    mailbox = models.ForeignKey(Mailbox, null=False, blank=False, on_delete=models.CASCADE)
    source = models.ForeignKey(Station, null=False, blank=False, on_delete=models.CASCADE)
    message = models.CharField(max_length=512, null=False, blank=False)
    timestamp = models.DateTimeField(null=False, blank=False, auto_now=True)
    read = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False)

    @property
    def ts(self):
        return self.timestamp.strftime("%y-%m-%d %H:%M:%S")

    def __str__(self):
        return "{} -> {}: {}".format(
            self.source.station, self.mailbox.owner.station, self.message
        )

    def __repr__(self):
        return "<Message: {} -> {}: {}>".format(
            self.source.station, self.mailbox.owner.station, self.message
        )


class Chat(models.Model):
    name = models.CharField(max_length=10, null=False, blank=False)
    topic = models.CharField(max_length=32, null=True, blank=True)
    owner = models.ForeignKey(Station, null=False, blank=False, on_delete=models.CASCADE)

    @property
    def members(self):
        members = []

        for member in ChatSubscription.objects.filter(chat=self):
            members.append(member.station)

        return members

    @property
    def messages(self):
        return self.chatmessage_set.all()

    def __str__(self):
        return self.name

    def __repr__(self):
        return "<Chat: {}>".format(self.name)


class ChatMessage(models.Model):
    chat = models.ForeignKey(Chat, null=False, blank=False, on_delete=models.CASCADE)
    source = models.ForeignKey(Station, null=False, blank=False, on_delete=models.CASCADE)
    message = models.CharField(max_length=512, null=True, blank=True)
    timestamp = models.DateTimeField(null=False, blank=False, auto_now=True)

    @property
    def ts(self):
        return self.timestamp.strftime("%y-%m-%d %H:%M:%S")

    def __str__(self):
        return "{} -> {}: {}".format(self.source.station, self.chat.name, self.message)

    def __repr__(self):
        return "<ChatMessage: {} -> {}: {}>".format(
            self.source.station, self.chat.name, self.message
        )


class ChatSubscription(models.Model):
    chat = models.ForeignKey(Chat, null=False, blank=False, on_delete=models.CASCADE)
    station = models.ForeignKey(Station, null=False, blank=False, on_delete=models.CASCADE)

    def __str__(self):
        return "{} <-> {}".format(self.station.station, self.chat.name)

    def __repr__(self):
        return "<ChatSubscription: {} -> {}>".format(self.station.station, self.chat.name)


class Command(models.Model):
    source = models.ForeignKey(Station, null=False, blank=False, on_delete=models.CASCADE)
    command = models.CharField(max_length=512, null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True, auto_now=True)
    response = models.BooleanField(default=False)

    @property
    def ts(self):
        return self.timestamp.strftime("%y-%m-%d %H:%M:%S")

    def __str__(self):
        return "{}: {}".format(self.source.station, self.command)

    def __repr__(self):
        return "<Command: {}: {}>".format(self.source.station, self.command)
