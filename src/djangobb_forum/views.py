# -*- coding: utf-8 -*-

import math
from datetime import datetime, timedelta

from django.shortcuts import get_object_or_404
from django.http import Http404, HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.core.cache import cache
from django.db.models import Q, F, Sum
from django.utils.encoding import smart_str
from django.utils import timezone
from django.db import transaction

from haystack.query import SearchQuerySet, SQ

from .. accounts.models import User
from . import settings as forum_settings
from . import models
from . import util
from . import forms
from . templatetags.forum_extras import forum_moderated_by, forum_editable_by, has_unreads


@util.render_to('forum/index.html')
def index(request, full=True):
    users_cached = cache.get('users_online', {})
    users_online = users_cached and User.objects.filter(id__in=users_cached.keys()) or []
    guests_cached = cache.get('guests_online', {})
    guest_count = len(guests_cached)
    users_count = len(users_online)

    cats = {}
    forums = {}
    user_groups = request.user.groups.all()
    if request.user.is_anonymous():  # in django 1.1 EmptyQuerySet raise exception
        user_groups = []
    _forums = models.Forum.objects.filter(
            Q(category__groups__in=user_groups) | \
            Q(category__groups__isnull=True)).select_related('last_post__topic',
                                                            'last_post__user',
                                                            'category')
    for forum in _forums:
        cat = cats.setdefault(forum.category.id,
            {'id': forum.category.id, 'cat': forum.category, 'forums': []})
        cat['forums'].append(forum)
        forums[forum.id] = forum

    cmpdef = lambda a, b: cmp(a['cat'].position, b['cat'].position)
    cats = sorted(cats.values(), cmpdef)

    to_return = {'cats': cats,
                'posts': models.Post.objects.count(),
                'topics': models.Topic.objects.count(),
                'users': User.objects.count(),
                'users_online': users_online,
                'online_count': users_count,
                'guest_count': guest_count,
                'last_user': User.objects.latest('date_joined'),
                }
    if full:
        return to_return
    else:
        to_return['TEMPLATE'] = 'forum/lofi/index.html'
        return to_return


@transaction.commit_on_success
@util.render_to('forum/moderate.html')
@util.paged('topics', forum_settings.FORUM_PAGE_SIZE)
def moderate(request, forum_id):
    forum = get_object_or_404(models.Forum, pk=forum_id)
    topics = forum.topics.order_by('-sticky', '-updated').select_related()
    if request.user.is_superuser or request.user in forum.moderators.all():
        topic_ids = request.POST.getlist('topic_id')
        if 'move_topics' in request.POST:
            return {
                'categories': models.Category.objects.all(),
                'topic_ids': topic_ids,
                'exclude_forum': forum,
                'TEMPLATE': 'forum/move_topic.html'
            }
        elif 'delete_topics' in request.POST:
            for topic_id in topic_ids:
                topic = get_object_or_404(models.Topic, pk=topic_id)
                topic.delete()
            return HttpResponseRedirect(reverse('djangobb:index'))
        elif 'open_topics' in request.POST:
            for topic_id in topic_ids:
                open_close_topic(request, topic_id)
            return HttpResponseRedirect(reverse('djangobb:index'))
        elif 'close_topics' in request.POST:
            for topic_id in topic_ids:
                open_close_topic(request, topic_id)
            return HttpResponseRedirect(reverse('djangobb:index'))

        return {'forum': forum,
                'topics': topics,
                #'sticky_topics': forum.topics.filter(sticky=True),
                'paged_qs': topics,
                'posts': forum.posts.count(),
                }
    else:
        raise Http404


@util.render_to('forum/search_topics.html')
@util.paged('results', forum_settings.SEARCH_PAGE_SIZE)
def search(request):
    # TODO: move to form
    user = request.user
    if 'action' in request.GET:
        action = request.GET['action']
        #FIXME: show_user for anonymous raise exception,
        #django bug http://code.djangoproject.com/changeset/14087 :|
        groups = request.user.groups.all() or []  # removed after django > 1.2.3 release
        topics = models.Topic.objects.filter(
                   Q(forum__category__groups__in=groups) | \
                   Q(forum__category__groups__isnull=True))
        if action == 'show_24h':
            date = datetime.today() - timedelta(1)
            topics = topics.filter(created__gte=date)
        elif action == 'show_new' and user.is_authenticated():
            last_read = models.PostTracking.objects.get(user=request.user).last_read
            last_read = None
            if last_read:
                topics = topics.filter(last_post__updated__gte=last_read).all()
            else:
                #searching more than forum_settings.SEARCH_PAGE_SIZE in this way - not good idea :]
                topics = [topic for topic in topics[:forum_settings.SEARCH_PAGE_SIZE] \
                    if has_unreads(topic, request.user)]
        elif action == 'show_unanswered':
            topics = topics.filter(post_count=1)
        elif action == 'show_subscriptions' and user.is_authenticated():
            topics = topics.filter(subscribers=request.user)
        elif action == 'show_user':
            user_id = request.GET.get('user_id')
            if user_id:
                posts = models.Post.objects.filter(user__id=user_id)
                topics = [post.topic for post in posts if post.topic in topics]
        elif action == 'search':
            keywords = request.GET.get('keywords')
            author = request.GET.get('author')
            forum = request.GET.get('forum')
            search_in = request.GET.get('search_in')
            sort_by = request.GET.get('sort_by')
            sort_dir = request.GET.get('sort_dir')

            if not (keywords or author):
                return HttpResponseRedirect(reverse('djangobb:search'))

            query = SearchQuerySet().models(models.Post)

            if author:
                query = query.filter(author__id=author)

            if forum != u'0':
                query = query.filter(forum__id=forum)

            if keywords:
                if search_in == 'all':
                    query = query.filter(SQ(topic=keywords) | SQ(text=keywords))
                elif search_in == 'messsage':
                    query = query.filter(text=keywords)
                elif search_in == 'topic':
                    query = query.filter(topic=keywords)

            order = {'0': 'created',
                     '1': 'user',
                     '2': 'topic',
                     '3': 'forum'}.get(sort_by, 'created')
            if sort_dir == 'DESC':
                order = '-' + order

            posts = query.order_by(order)

            if 'topics' == request.GET.get('show_as'):
                topics = []
                topics_to_exclude = None
                for post in posts:
                    if post.object and post.object.topic not in topics:
                        if post.object.topic.forum.category.has_access(request.user):
                            topics.append(post.object.topic)
                        else:
                            if topics_to_exclude:
                                topics_to_exclude |= SQ(topic=post.object.topic)
                            else:
                                topics_to_exclude = SQ(topic=post.object.topic)

                if topics_to_exclude:
                    posts = posts.exclude(topics_to_exclude)
                return {'paged_qs': topics}
            elif 'posts' == request.GET.get('show_as'):
                return {'paged_qs': posts,
                        'TEMPLATE': 'forum/search_posts.html'
                        }
        return {'paged_qs': topics}
    else:
        form = forms.PostSearchForm()
        return {'categories': models.Category.objects.all(),
                'form': form,
                'TEMPLATE': 'forum/search_form.html'
                }


@login_required
@util.render_to('forum/report.html')
def misc(request):
    if 'action' in request.GET:
        action = request.GET['action']
        if action == 'markread':
            user = request.user
            models.PostTracking.objects.filter(user__id=user.id).update(last_read=timezone.now(), topics=None)
            return HttpResponseRedirect(reverse('djangobb:index'))

        elif action == 'report':
            if request.GET.get('post_id', ''):
                post_id = request.GET['post_id']
                post = get_object_or_404(models.Post, id=post_id)
                form = util.build_form(forms.ReportForm, request, reported_by=request.user, post=post_id)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(post.get_absolute_url())
                return {'form': form}

    elif 'submit' in request.POST and 'mail_to' in request.GET:
        form = forms.MailToForm(request.POST)
        if form.is_valid():
            user = get_object_or_404(User, username=request.GET['mail_to'])
            subject = form.cleaned_data['subject']
            body = form.cleaned_data['body'] + '\n %s %s [%s]' % (Site.objects.get_current().domain,
                                                                  request.user.username,
                                                                  request.user.email)
            user.email_user(subject, body, request.user.email)
            return HttpResponseRedirect(reverse('djangobb:index'))

    elif 'mail_to' in request.GET:
        user = get_object_or_404(User, username=request.GET['mail_to'])
        form = forms.MailToForm()
        return {'form': form,
                'user': user,
               'TEMPLATE': 'forum/mail_to.html'
               }


@util.render_to('forum/forum.html')
@util.paged('topics', forum_settings.FORUM_PAGE_SIZE)
def show_forum(request, forum_id, full=True):
    forum = get_object_or_404(models.Forum, pk=forum_id)
    if not forum.category.has_access(request.user):
        return HttpResponseForbidden()
    topics = forum.topics.order_by('-sticky', '-updated').select_related()
    moderator = request.user.is_superuser or\
        request.user in forum.moderators.all()
    to_return = {'categories': models.Category.objects.all(),
                'forum': forum,
                'paged_qs': topics,
                'posts': forum.post_count,
                'topics': forum.topic_count,
                'moderator': moderator,
                }
    if full:
        return to_return
    else:
        pages, paginator, paged_list_name = util.paginate(topics, request, forum_settings.FORUM_PAGE_SIZE)
        to_return.update({'pages': pages,
                        'paginator': paginator,
                        'topics': paged_list_name,
                        'TEMPLATE': 'forum/lofi/forum.html'
                        })
        del to_return['paged_qs']
        return to_return


@transaction.commit_on_success
@util.render_to('forum/topic.html')
@util.paged('posts', forum_settings.TOPIC_PAGE_SIZE)
def show_topic(request, topic_id, full=True):
    topic = get_object_or_404(models.Topic.objects.select_related(), pk=topic_id)
    if not topic.forum.category.has_access(request.user):
        return HttpResponseForbidden()
    models.Topic.objects.filter(pk=topic.id).update(views=F('views') + 1)

    last_post = topic.last_post

    if request.user.is_authenticated():
        topic.update_read(request.user)

    posts = topic.posts.all().select_related()

    users = set(post.user.id for post in posts)
    profiles = models.Profile.objects.filter(user__pk__in=users)
    profiles = dict((profile.user_id, profile) for profile in profiles)

    for post in posts:
        post.user.forum_profile = profiles[post.user.id]

    if forum_settings.REPUTATION_SUPPORT:
        #values_list buggy?
        replies_list = models.Reputation.objects.filter(to_user__pk__in=users).values('to_user_id').annotate(sign=Sum('sign'))

        replies = {}
        for r in replies_list:
            replies[r['to_user_id']] = r['sign']

        for post in posts:
            post.user.forum_profile.reply_total = replies.get(post.user.id, 0)

    initial = {}
    if request.user.is_authenticated():
        initial = {'markup': request.user.forum_profile.markup}
    form = forms.AddPostForm(topic=topic, initial=initial)

    moderator = request.user.is_superuser or\
        request.user in topic.forum.moderators.all()
    if request.user.is_authenticated() and request.user in topic.subscribers.all():
        subscribed = True
    else:
        subscribed = False

    highlight_word = request.GET.get('hl', '')
    context = dict(
        categories=models.Category.objects.all(),
        topic=topic,
        )
    if full:
        return dict(context,
            last_post=last_post,
            form=form,
            moderator=moderator,
            subscribed=subscribed,
            paged_qs=posts,
            highlight_word=highlight_word)
    else:
        pages, paginator, paged_list_name = util.paginate(posts, request, forum_settings.TOPIC_PAGE_SIZE)
        return dict(context,
            pages=pages,
            paginator=paginator,
            posts=paged_list_name,
            TEMPLATE='forum/lofi/topic.html')


@login_required
@transaction.commit_on_success
@util.render_to('forum/add_post.html')
def add_post(request, forum_id, topic_id):
    forum = None
    topic = None
    posts = None

    if forum_id:
        forum = get_object_or_404(models.Forum, pk=forum_id)
        if not forum.category.has_access(request.user):
            return HttpResponseForbidden()
    elif topic_id:
        topic = get_object_or_404(models.Topic, pk=topic_id)
        posts = topic.posts.all().select_related()
        if not topic.forum.category.has_access(request.user):
            return HttpResponseForbidden()
    if topic and topic.closed:
        return HttpResponseRedirect(topic.get_absolute_url())

    ip = request.META.get('REMOTE_ADDR', None)
    form = util.build_form(forms.AddPostForm, request, topic=topic, forum=forum,
                      user=request.user, ip=ip,
                      initial={'markup': request.user.forum_profile.markup})

    if 'post_id' in request.GET:
        post_id = request.GET['post_id']
        post = get_object_or_404(models.Post, pk=post_id)
        form.fields['body'].initial = "[quote=%s]%s[/quote]" % (post.user, post.body)

    if form.is_valid():
        post = form.save()
        return HttpResponseRedirect(post.get_absolute_url())

    return {'form': form,
            'posts': posts,
            'topic': topic,
            'forum': forum,
            }


@transaction.commit_on_success
@util.render_to('forum/user.html')
def user(request, username):
    user = get_object_or_404(User, username=username)
    if request.user.is_authenticated() and user == request.user or request.user.is_superuser:
        if 'section' in request.GET:
            section = request.GET['section']
            profile_url = reverse('djangobb:forum_profile', args=[user.username]) + '?section=' + section
            if section == 'privacy':
                form = util.build_form(forms.PrivacyProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return {'active_menu': 'privacy',
                        'profile': user,
                        'form': form,
                        'TEMPLATE': 'forum/profile/profile_privacy.html'
                       }
            elif section == 'display':
                form = util.build_form(forms.DisplayProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return {'active_menu': 'display',
                        'profile': user,
                        'form': form,
                        'TEMPLATE': 'forum/profile/profile_display.html'
                       }
            elif section == 'personality':
                form = util.build_form(forms.PersonalityProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return {'active_menu': 'personality',
                        'profile': user,
                        'form': form,
                        'TEMPLATE': 'forum/profile/profile_personality.html'
                        }
            elif section == 'messaging':
                form = util.build_form(forms.MessagingProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return {'active_menu': 'messaging',
                        'profile': user,
                        'form': form,
                        'TEMPLATE': 'forum/profile/profile_messaging.html'
                       }
            elif section == 'personal':
                form = util.build_form(forms.PersonalProfileForm, request, instance=user.forum_profile, user=user)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return {'active_menu': 'personal',
                        'profile': user,
                        'form': form,
                        'TEMPLATE': 'forum/profile/profile_personal.html'
                       }
            elif section == 'essentials':
                form = util.build_form(forms.EssentialsProfileForm, request, instance=user.forum_profile,
                                  user_view=user, user_request=request.user)
                if request.method == 'POST' and form.is_valid():
                    profile = form.save()
                    util.set_language(request, profile.language)
                    return HttpResponseRedirect(profile_url)

                return {'active_menu': 'essentials',
                        'profile': user,
                        'form': form,
                        'TEMPLATE': 'forum/profile/profile_essentials.html'
                        }
        else:
            form = util.build_form(forms.EssentialsProfileForm, request, instance=user.forum_profile,
                                  user_view=user, user_request=request.user)
            if request.method == 'POST' and form.is_valid():
                profile = form.save()
                util.set_language(request, profile.language)
                return HttpResponseRedirect(reverse('djangobb:forum_profile', args=[user.username]))
            return {'active_menu': 'essentials',
                    'profile': user,
                    'form': form,
                    'TEMPLATE': 'forum/profile/profile_essentials.html'
                   }

    else:
        topic_count = models.Topic.objects.filter(user__id=user.id).count()
        if user.forum_profile.post_count < forum_settings.POST_USER_SEARCH and not request.user.is_authenticated():
            return HttpResponseRedirect(reverse('accounts:login') + '?next=%s' % request.path)
        return {'profile': user,
                'topic_count': topic_count,
               }


@login_required
@transaction.commit_on_success
@util.render_to('forum/reputation.html')
def reputation(request, username):
    user = get_object_or_404(User, username=username)
    form = util.build_form(forms.ReputationForm, request, from_user=request.user, to_user=user)

    if 'action' in request.GET:
        if request.user == user:
            return HttpResponseForbidden(u'You can not change the reputation of yourself')

        if 'post_id' in request.GET:
            post_id = request.GET['post_id']
            form.fields['post'].initial = post_id
            if request.GET['action'] == 'plus':
                form.fields['sign'].initial = 1
            elif request.GET['action'] == 'minus':
                form.fields['sign'].initial = -1
            return {'form': form,
                    'TEMPLATE': 'forum/reputation_form.html'
                    }
        else:
            raise Http404

    elif request.method == 'POST':
        if 'del_reputation' in request.POST and request.user.is_superuser:
            reputation_list = request.POST.getlist('reputation_id')
            for reputation_id in reputation_list:
                    reputation = get_object_or_404(models.Reputation, pk=reputation_id)
                    reputation.delete()
            return HttpResponseRedirect(reverse('djangobb:index'))
        elif form.is_valid():
            form.save()
            post_id = request.POST['post']
            post = get_object_or_404(models.Post, id=post_id)
            return HttpResponseRedirect(post.get_absolute_url())
        else:
            return {'form': form,
                    'TEMPLATE': 'forum/reputation_form.html'
                    }
    else:
        reputations = models.Reputation.objects.filter(to_user__id=user.id).order_by('-time').select_related()
        return {'reputations': reputations,
                'profile': user.forum_profile,
               }


def show_post(request, post_id):
    post = get_object_or_404(models.Post, pk=post_id)
    count = post.topic.posts.filter(created__lt=post.created).count() + 1
    page = math.ceil(count / float(forum_settings.TOPIC_PAGE_SIZE))
    url = '%s?page=%d#post-%d' % (reverse('djangobb:topic', args=[post.topic.id]), page, post.id)
    return HttpResponseRedirect(url)


@login_required
@transaction.commit_on_success
@util.render_to('forum/edit_post.html')
def edit_post(request, post_id):
    post = get_object_or_404(models.Post, pk=post_id)
    topic = post.topic
    if not forum_editable_by(post, request.user):
        return HttpResponseRedirect(post.get_absolute_url())
    form = util.build_form(forms.EditPostForm, request, topic=topic, instance=post)
    if form.is_valid():
        post = form.save(commit=False)
        post.updated_by = request.user
        post.save()
        return HttpResponseRedirect(post.get_absolute_url())

    return {'form': form,
            'post': post,
            }


@login_required
@transaction.commit_on_success
@util.render_to('forum/delete_posts.html')
@util.paged('posts', forum_settings.TOPIC_PAGE_SIZE)
def delete_posts(request, topic_id):

    topic = models.Topic.objects.select_related().get(pk=topic_id)

    if forum_moderated_by(topic, request.user):
        deleted = False
        post_list = request.POST.getlist('post')
        for post_id in post_list:
            if not deleted:
                deleted = True
            delete_post(request, post_id)
        if deleted:
            return HttpResponseRedirect(topic.get_absolute_url())

    last_post = topic.posts.latest()

    if request.user.is_authenticated():
        topic.update_read(request.user)

    posts = topic.posts.all().select_related()

    profiles = models.Profile.objects.filter(user__pk__in=set(x.user.id for x in posts))
    profiles = dict((x.user_id, x) for x in profiles)

    for post in posts:
        post.user.forum_profile = profiles[post.user.id]

    initial = {}
    if request.user.is_authenticated():
        initial = {'markup': request.user.forum_profile.markup}
    form = forms.AddPostForm(topic=topic, initial=initial)

    moderator = request.user.is_superuser or\
        request.user in topic.forum.moderators.all()
    if request.user.is_authenticated() and request.user in topic.subscribers.all():
        subscribed = True
    else:
        subscribed = False
    return {
            'topic': topic,
            'last_post': last_post,
            'form': form,
            'moderator': moderator,
            'subscribed': subscribed,
            'paged_qs': posts,
            }


@login_required
@transaction.commit_on_success
@util.render_to('forum/move_topic.html')
def move_topic(request):
    if 'topic_id' in request.GET:
        #if move only 1 topic
        topic_ids = [request.GET['topic_id']]
    else:
        topic_ids = request.POST.getlist('topic_id')
    first_topic = topic_ids[0]
    topic = get_object_or_404(models.Topic, pk=first_topic)
    from_forum = topic.forum
    if 'to_forum' in request.POST:
        to_forum_id = int(request.POST['to_forum'])
        to_forum = get_object_or_404(models.Forum, pk=to_forum_id)
        for topic_id in topic_ids:
            topic = get_object_or_404(models.Topic, pk=topic_id)
            if topic.forum != to_forum:
                if forum_moderated_by(topic, request.user):
                    topic.forum = to_forum
                    topic.save()

        #TODO: not DRY
        try:
            last_post = models.Post.objects.filter(topic__forum__id=from_forum.id).latest()
        except models.Post.DoesNotExist:
            last_post = None
        from_forum.last_post = last_post
        from_forum.topic_count = from_forum.topics.count()
        from_forum.post_count = from_forum.posts.count()
        from_forum.save()
        return HttpResponseRedirect(to_forum.get_absolute_url())

    return {'categories': models.Category.objects.all(),
            'topic_ids': topic_ids,
            'exclude_forum': from_forum,
            }


@login_required
@transaction.commit_on_success
def stick_unstick_topic(request, topic_id):

    topic = get_object_or_404(models.Topic, pk=topic_id)
    if forum_moderated_by(topic, request.user):
        topic.sticky = not topic.sticky
        topic.save()
    return HttpResponseRedirect(topic.get_absolute_url())


@login_required
@transaction.commit_on_success
def make_heresy(request, topic_id):

    topic = get_object_or_404(models.Topic, pk=topic_id)
    if forum_moderated_by(topic, request.user):
        topic.heresy = not topic.heresy
        topic.save()
    return HttpResponseRedirect(topic.get_absolute_url())


@login_required
@transaction.commit_on_success
@util.render_to('forum/delete_post.html')
def delete_post(request, post_id):
    post = get_object_or_404(models.Post, pk=post_id)
    last_post = post.topic.last_post
    topic = post.topic
    forum = post.topic.forum

    allowed = False
    if request.user.is_superuser or\
        request.user in post.topic.forum.moderators.all() or \
        (post.user == request.user and post == last_post):
        allowed = True

    if not allowed:
        return HttpResponseRedirect(post.get_absolute_url())

    post.delete()

    try:
        models.Topic.objects.get(pk=topic.id)
    except models.Topic.DoesNotExist:
        #removed latest post in topic
        return HttpResponseRedirect(forum.get_absolute_url())
    else:
        return HttpResponseRedirect(topic.get_absolute_url())


@login_required
@transaction.commit_on_success
def open_close_topic(request, topic_id):

    topic = get_object_or_404(models.Topic, pk=topic_id)
    if forum_moderated_by(topic, request.user):
        topic.closed = not topic.closed
        topic.save()
    return HttpResponseRedirect(topic.get_absolute_url())


@util.render_to('forum/users.html')
@util.paged('users', forum_settings.USERS_PAGE_SIZE)
def users(request):
    users = User.objects.filter(forum_profile__post_count__gte=forum_settings.POST_USER_SEARCH).order_by('username')
    form = forms.UserSearchForm(request.GET)
    users = form.filter(users)
    return {'paged_qs': users,
            'form': form,
            }


@login_required
@transaction.commit_on_success
def delete_subscription(request, topic_id):
    topic = get_object_or_404(models.Topic, pk=topic_id)
    topic.subscribers.remove(request.user)
    if 'from_topic' in request.GET:
        return HttpResponseRedirect(reverse('djangobb:topic', args=[topic.id]))
    else:
        return HttpResponseRedirect(reverse('djangobb:edit_profile'))


@login_required
@transaction.commit_on_success
def add_subscription(request, topic_id):
    topic = get_object_or_404(models.Topic, pk=topic_id)
    topic.subscribers.add(request.user)
    return HttpResponseRedirect(reverse('djangobb:topic', args=[topic.id]))


@login_required
def show_attachment(request, hash):
    attachment = get_object_or_404(models.Attachment, hash=hash)
    file_data = file(attachment.get_absolute_path(), 'rb').read()
    response = HttpResponse(file_data, mimetype=attachment.content_type)
    response['Content-Disposition'] = 'attachment; filename=%s' % smart_str(attachment.name)
    return response


@login_required
#@require_POST
@util.render_to('forum/post_preview.html')
def post_preview(request):
    '''Preview for markitup'''
    markup = request.user.forum_profile.markup
    data = request.POST.get('data', '')

    data = util.convert_text_to_html(data, markup)
    if forum_settings.SMILES_SUPPORT:
        data = util.smiles(data)
    return {'data': data}
