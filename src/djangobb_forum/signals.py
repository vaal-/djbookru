# -*- coding: utf-8 -*-

from django.db.models.signals import post_save
from django.utils import timezone

from . subscription import notify_topic_subscribers
from . models import Topic, Post


def post_saved(instance, **kwargs):
    if not kwargs.get('raw'):
        created = kwargs.get('created')
        post = instance
        topic = post.topic

        if created:
            topic.last_post = post
            topic.post_count = topic.posts.count()
            topic.updated = timezone.now()
            profile = post.user.forum_profile
            profile.post_count = post.user.posts.count()
            profile.save(force_update=True)
            notify_topic_subscribers(post)
        topic.save(force_update=True)


def topic_saved(instance, **kwargs):
    topic = instance
    forum = topic.forum
    forum.topic_count = forum.topics.count()
    forum.updated = topic.updated
    forum.post_count = forum.posts.count()
    forum.last_post_id = topic.last_post_id
    forum.save(force_update=True)


post_save.connect(post_saved, sender=Post)
post_save.connect(topic_saved, sender=Topic)
