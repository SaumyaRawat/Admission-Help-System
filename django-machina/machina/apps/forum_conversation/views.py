# -*- coding: utf-8 -*-

from __future__ import unicode_literals

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _
from django.views.generic import DeleteView
from django.views.generic import FormView
from django.views.generic import ListView
from django.views.generic import UpdateView
from django.views.generic import DetailView
from django.views.generic.edit import ModelFormMixin
from django.views.generic.detail import SingleObjectMixin
from django_mysql.models import SetTextField

from machina.apps.forum_conversation.signals import topic_viewed
from machina.apps.forum_conversation.utils import get_client_ip
from machina.conf import settings as machina_settings
from machina.core.db.models import get_model
from machina.core.loading import get_class

import re

FLAG_THRESHOLD = 5

from nltk.corpus import stopwords
stop_words = stopwords.words('english') + [""]

from nltk.stem.porter import PorterStemmer
porter = PorterStemmer()

Attachment = get_model('forum_attachments', 'Attachment')
Forum = get_model('forum', 'Forum')
Post = get_model('forum_conversation', 'Post')
Topic = get_model('forum_conversation', 'Topic')
TopicPollOption = get_model('forum_polls', 'TopicPollOption')

AttachmentFormset = get_class('forum_attachments.forms', 'AttachmentFormset')
PostForm = get_class('forum_conversation.forms', 'PostForm')
TopicForm = get_class('forum_conversation.forms', 'TopicForm')
TopicPollOptionFormset = get_class('forum_polls.forms', 'TopicPollOptionFormset')
TopicPollVoteForm = get_class('forum_polls.forms', 'TopicPollVoteForm')
PostUpvoteForm = get_class('forum_conversation.forms', 'PostUpvoteForm')

attachments_cache = get_class('forum_attachments.cache', 'cache')

PermissionRequiredMixin = get_class('forum_permission.viewmixins', 'PermissionRequiredMixin')


class TopicView(PermissionRequiredMixin, ListView):
    """
    Displays a forum topic.
    """
    template_name = 'forum_conversation/topic_detail.html'
    context_object_name = 'posts'
    permission_required = ['can_read_forum', ]
    paginate_by = machina_settings.TOPIC_POSTS_NUMBER_PER_PAGE
    view_signal = topic_viewed

    poll_form_class = TopicPollVoteForm

    def get(self, request, **kwargs):
        topic = self.get_topic()

        # Handle pagination
        requested_post = request.GET.get('post', None)
        if requested_post:
            try:
                assert requested_post.isdigit()
                post = topic.posts.get(pk=requested_post)
                requested_page = ((post.position - 1) //
                                  machina_settings.TOPIC_POSTS_NUMBER_PER_PAGE) + 1
                request.GET = request.GET.copy()  # A QueryDict is immutable
                request.GET.update({'page': requested_page})
            except (Post.DoesNotExist, AssertionError):
                pass

        response = super(TopicView, self).get(request, **kwargs)
        self.send_signal(request, response, topic)
        return response

    def get_topic(self):
        """
        Returns the topic to consider.
        """
        if not hasattr(self, 'topic'):
            self.topic = get_object_or_404(
                Topic.objects.select_related('forum').all(), pk=self.kwargs['pk'])
        return self.topic

    def get_queryset(self):
        self.topic = self.get_topic()
        qs = self.topic.posts.all().exclude(approved=False) \
            .select_related('poster', 'updated_by') \
            .prefetch_related('attachments', 'poster__forum_profile')
        return qs

    def get_controlled_object(self):
        return self.get_topic().forum

    def get_context_data(self, **kwargs):
        context = super(TopicView, self).get_context_data(**kwargs)

        # Insert the considered topic and the associated forum into the context
        topic = self.get_topic()
        context['topic'] = topic
        context['forum'] = topic.forum

        # Handles the case when a poll is associated to the topic
        try:
            if hasattr(topic, 'poll') and topic.poll.options.exists():
                context['poll'] = topic.poll
                context['poll_form'] = self.poll_form_class(poll=topic.poll)
                context['view_results_action'] = self.request.GET.get('view_results', None)
                context['change_vote_action'] = self.request.GET.get('change_vote', None)
        except ObjectDoesNotExist:  # pragma: no cover
            pass

        return context

    def send_signal(self, request, response, topic):
        self.view_signal.send(
            sender=self, topic=topic, user=request.user,
            request=request, response=response)


class BasePostFormView(FormView):
    """
    A base view for handling post forms.
    """
    post_form_class = PostForm
    attachment_formset_class = AttachmentFormset

    post_pk_url_kwarg = None
    topic_pk_url_kwarg = None
    forum_pk_url_kwarg = None

    success_message = _('This message has been posted successfully.')
    approval_required_message = _('This message will be validated before appearing on the forum.')
    attachment_formset_general_error_message = _(
        'There are some errors in the attachments you submitted.')
    duplicate_error_message = _('Post was not created as it was detected as duplicate.')

    def get(self, request, *args, **kwargs):
        self.init_attachment_cache()

        # Initializes the forms
        post_form_class = self.get_post_form_class()
        post_form = self.get_post_form(post_form_class)
        attachment_formset_class = self.get_attachment_formset_class()
        attachment_formset = self.get_attachment_formset(attachment_formset_class)

        return self.render_to_response(
            self.get_context_data(
                post_form=post_form,
                attachment_formset=attachment_formset))

    def clean_text(self, text):
        if not text:
            return {}
        text = text.lower()
        words = re.split(r'\W+', text)
        words = [w for w in words if not w in stop_words]
        stemmed = [porter.stem(word) for word in words]
        return {token for token in stemmed}

    def compute_jaccard_similarity(self, tokens1, tokens2):
        if tokens1 is None or tokens2 is None:
            return 0
        intersection = tokens1 & tokens2
        union = tokens1 | tokens2
        return len(intersection) / len(union)

    def post(self, request, *args, **kwargs):
        self.init_attachment_cache()

        # Stores a boolean indicating if we are considering a preview
        self.preview = 'preview' in self.request.POST

        text = self.request.POST['content']
        self.tokens = self.clean_text(text)
        self.duplicate = False

        forum = self.get_forum()
        topics = forum.topics.all()
        for topic in topics:
            if hasattr(topic, "tokens"):
                topic_tokens = topic.tokens
                jsm = self.compute_jaccard_similarity(topic_tokens, self.tokens)
                if jsm > 0.7:
                    self.duplicate = True
            posts = topic.posts.all()
            for post in posts:
                if hasattr(post, "tokens"):
                    post_tokens = post.tokens
                    jsm = self.compute_jaccard_similarity(post_tokens, self.tokens)
                    if jsm > 0.7:
                        self.duplicate = True

        # Initializes the forms
        post_form_class = self.get_post_form_class()
        post_form = self.get_post_form(post_form_class)
        attachment_formset_class = self.get_attachment_formset_class()
        attachment_formset = self.get_attachment_formset(attachment_formset_class)

        self.attachment_preview = self.preview if attachment_formset \
            and attachment_formset.is_valid() else None

        post_form_valid = post_form.is_valid()
        if self.duplicate == True:
            post_form_valid = False

        if (post_form_valid and attachment_formset is None) or \
                (post_form_valid and attachment_formset.is_valid()):
            return self.form_valid(post_form, attachment_formset)
        else:
            return self.form_invalid(post_form, attachment_formset)

    def init_attachment_cache(self):
        """
        Initializes the attachment cache for the current view.
        """
        if self.request.method == 'GET':
            # Invalidates previous attachments
            attachments_cache.delete(self.get_attachments_cache_key(self.request))
            return

        # Try to restore previous uploaded attachments if applicable
        attachments_cache_key = self.get_attachments_cache_key(self.request)
        restored_attachments_dict = attachments_cache.get(attachments_cache_key)
        if restored_attachments_dict:
            restored_attachments_dict.update(self.request.FILES)
            self.request._files = restored_attachments_dict

        # Updates the attachment cache if files are available
        if self.request.FILES:
            attachments_cache.set(attachments_cache_key, self.request.FILES)

    def get_attachments_cache_key(self, request):
        """
        Returns the key used to store attachment files states into the
        file based cache.
        """
        return 'attachments_{}'.format(request.session.session_key)

    def get_post_form_class(self):
        """
        Returns the post form class to use for instantiating the form.
        """
        return self.post_form_class

    def get_post_form(self, form_class):
        """
        Returns an instance of the post form to be used in the view.
        """
        return form_class(**self.get_post_form_kwargs())

    def get_post_form_kwargs(self):
        """
        Returns the keyword arguments for instantiating the post form.
        """
        kwargs = {
            'user': self.request.user,
            'user_ip': get_client_ip(self.request),
            'forum': self.get_forum(),
            'topic': self.get_topic(),
        }

        post = self.get_post()
        if post:
            kwargs.update({'instance': post})

        if self.request.method in ('POST', 'PUT'):
            kwargs.update({
                'data': self.request.POST,
                'files': self.request.FILES,
            })
        return kwargs

    def get_attachment_formset_class(self):
        """
        Returns the attachment formset class to use for instantiating the
        attachment formset.
        """
        return self.attachment_formset_class

    def get_attachment_formset(self, formset_class):
        """
        Returns an instance of the attachment formset to be used in the view.
        """
        if self.request.forum_permission_handler.can_attach_files(
                self.get_forum(), self.request.user):
            return formset_class(**self.get_attachment_formset_kwargs())

    def get_attachment_formset_kwargs(self):
        """
        Returns the keyword arguments for instantiating the attachment formset.
        """
        kwargs = {
            'prefix': 'attachment',
        }

        if self.request.method in ('POST', 'PUT'):
            kwargs.update({
                'data': self.request.POST,
                'files': self.request.FILES,
            })
        else:
            post = self.get_post()
            attachment_queryset = Attachment.objects.filter(post=post)
            kwargs.update({
                'queryset': attachment_queryset,
            })
        return kwargs

    def get_context_data(self, **kwargs):
        context = kwargs
        if 'view' not in context:
            context['view'] = self

        # Insert the considered forum, topic and post into the context
        context['forum'] = self.get_forum()
        context['topic'] = self.get_topic()
        context['post'] = self.get_post()

        # Handles the preview of the attachments
        if context['attachment_formset']:
            if hasattr(self, 'attachment_preview') and self.attachment_preview:
                context['attachment_preview'] = self.attachment_preview
                attachments = []
                # Computes the list of the attachment file names that should be attached
                # to the forum post being created or updated
                for form in context['attachment_formset'].forms:
                    if form['DELETE'].value() \
                        or (not form['file'].html_name in self.request._files and
                            not form.instance.pk):
                        continue
                    attachments.append(
                        (
                            form,
                            self.request._files[form['file'].html_name].name if not form.instance
                            else form.instance.filename
                        ))
                context['attachment_file_previews'] = attachments

        return context

    def get_forum(self):
        """
        Returns the considered forum.
        """
        pk = self.kwargs.get(self.forum_pk_url_kwarg, None)
        if not pk:  # pragma: no cover
            # This should never happen
            return
        if not hasattr(self, '_forum'):
            self._forum = get_object_or_404(Forum, pk=pk)
        return self._forum

    def get_topic(self):
        """
        Returns the considered topic if applicable.
        """
        pk = self.kwargs.get(self.topic_pk_url_kwarg, None)
        if not pk:
            return
        if not hasattr(self, '_topic'):
            self._topic = get_object_or_404(Topic, pk=pk)
        return self._topic

    def get_post(self):
        """
        Returns the considered post if applicable.
        """
        pk = self.kwargs.get(self.post_pk_url_kwarg, None)
        if not pk:
            return
        if not hasattr(self, '_forum_post'):
            self._forum_post = get_object_or_404(Post, pk=pk)
        return self._forum_post

    def form_valid(self, post_form, attachment_formset, **kwargs):
        """
        Called if all forms are valid. Creates a Post instance along with
        associated attachments if required and then redirects to a success
        page.
        """
        save_attachment_formset = attachment_formset is not None \
            and not self.preview

        if self.preview:
            return self.render_to_response(
                self.get_context_data(
                    preview=True,
                    post_form=post_form,
                    attachment_formset=attachment_formset, **kwargs))

        if "tokens" in kwargs:
            self.tokens = kwargs.get("tokens")

        # This is not a preview ; the object is going to be saved
        self.forum_post = post_form.save(tokens=self.tokens)

        if save_attachment_formset:
            attachment_formset.post = self.forum_post
            attachment_formset.save()

        messages.success(self.request, self.success_message)
        if not self.forum_post.approved:
            messages.warning(self.request, self.approval_required_message)

        return HttpResponseRedirect(self.get_success_url())

    def form_invalid(self, post_form, attachment_formset, **kwargs):
        """
        Called if one of the forms is invalid. Re-renders the context data with
        the data-filled forms and errors.
        """
        if "duplicate" in kwargs:
            self.duplicate = kwargs.get("duplicate")

        if self.duplicate == True:
            messages.error(self.request, self.duplicate_error_message)
        if attachment_formset and not attachment_formset.is_valid() \
                and len(attachment_formset.errors):
            messages.error(
                self.request, self.attachment_formset_general_error_message)

        return self.render_to_response(
            self.get_context_data(
                post_form=post_form,
                attachment_formset=attachment_formset, **kwargs))


class BaseTopicFormView(BasePostFormView):
    """
    A base view for handling topic forms.
    """
    # A specific form class is used here in order to fill the Topic-related
    # date in addition to the ones related to Post instances.
    post_form_class = TopicForm

    poll_option_formset_class = TopicPollOptionFormset

    poll_option_formset_general_error_message = _(
        'There are some errors in the poll options you submitted.')

    def get(self, request, *args, **kwargs):
        self.init_attachment_cache()

        # Initializes the forms
        post_form_class = self.get_post_form_class()
        post_form = self.get_post_form(post_form_class)
        attachment_formset_class = self.get_attachment_formset_class()
        attachment_formset = self.get_attachment_formset(attachment_formset_class)
        poll_option_formset_class = self.get_poll_option_formset_class()
        poll_option_formset = self.get_poll_option_formset(poll_option_formset_class)

        return self.render_to_response(
            self.get_context_data(
                post_form=post_form,
                attachment_formset=attachment_formset,
                poll_option_formset=poll_option_formset))

    def post(self, request, *args, **kwargs):
        self.init_attachment_cache()

        # Stores a boolean indicating if we are considering a preview
        self.preview = 'preview' in self.request.POST
        text = self.request.POST['content']
        self.tokens = self.clean_text(text)
        self.duplicate = False

        forum = self.get_forum()
        topics = forum.topics.all()
        for topic in topics:
            if hasattr(topic, "tokens"):
                topic_tokens = topic.tokens
                jsm = self.compute_jaccard_similarity(topic_tokens, self.tokens)
                if jsm > 0.7:
                    self.duplicate = True            
            posts = topic.posts.all()
            for post in posts:
                if hasattr(post, "tokens"):
                    post_tokens = post.tokens
                    jsm = self.compute_jaccard_similarity(post_tokens, self.tokens)
                    if jsm > 0.7:
                        self.duplicate = True

        # Initializes the forms
        post_form_class = self.get_post_form_class()
        post_form = self.get_post_form(post_form_class)
        attachment_formset_class = self.get_attachment_formset_class()
        attachment_formset = self.get_attachment_formset(attachment_formset_class)
        poll_option_formset_class = self.get_poll_option_formset_class()
        poll_option_formset = self.get_poll_option_formset(poll_option_formset_class)

        post_form_valid = post_form.is_valid()
        if self.duplicate:
            post_form_valid = False

        attachment_formset_valid = attachment_formset.is_valid() if attachment_formset \
            else None
        poll_option_formset_valid = poll_option_formset.is_valid() if poll_option_formset \
            and len(post_form.cleaned_data['poll_question']) else None

        self.attachment_preview = self.preview if attachment_formset_valid else None
        self.poll_preview = self.preview if poll_option_formset_valid else None

        poll_options_validated = poll_option_formset_valid is not None           

        if post_form_valid and attachment_formset_valid is not False \
                and poll_option_formset_valid is not False:
            return self.form_valid(
                post_form, attachment_formset, poll_option_formset,
                poll_options_validated=poll_options_validated)
        else:
            return self.form_invalid(
                post_form, attachment_formset, poll_option_formset,
                poll_options_validated=poll_options_validated)

    def get_poll_option_formset_class(self):
        """
        Returns the poll option formset class to use for instantiating the
        poll option formset.
        """
        return self.poll_option_formset_class

    def get_poll_option_formset(self, formset_class):
        """
        Returns an instance of the poll option formset to be used in the view.
        """
        if self.request.forum_permission_handler.can_create_polls(
                self.get_forum(), self.request.user):
            return formset_class(**self.get_poll_option_formset_kwargs())

    def get_poll_option_formset_kwargs(self):
        """
        Returns the keyword arguments for instantiating the poll option formset.
        """
        kwargs = {
            'prefix': 'poll',
        }

        if self.request.method in ('POST', 'PUT'):
            kwargs.update({
                'data': self.request.POST,
                'files': self.request.FILES,
            })
        else:
            topic = self.get_topic()
            poll_option_queryset = TopicPollOption.objects.filter(poll__topic=topic)
            kwargs.update({
                'queryset': poll_option_queryset,
            })
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(BaseTopicFormView, self).get_context_data(**kwargs)

        # Handles the preview of the poll
        if context['poll_option_formset']:
            if hasattr(self, 'poll_preview') and self.poll_preview:
                context['poll_preview'] = self.poll_preview
                context['poll_options_previews'] = filter(
                    lambda f: f['text'].value() and not f['DELETE'].value(),
                    context['poll_option_formset'].forms)

        return context

    def form_valid(self, post_form, attachment_formset, poll_option_formset, **kwargs):
        save_poll_option_formset = poll_option_formset is not None \
            and not self.preview

        valid = super(BaseTopicFormView, self).form_valid(
            post_form, attachment_formset, poll_option_formset=poll_option_formset, tokens=self.tokens, **kwargs)

        if save_poll_option_formset:
            poll_option_formset.topic = self.forum_post.topic
            poll_option_formset.save(
                poll_question=post_form.cleaned_data.pop('poll_question', None),
                poll_max_options=post_form.cleaned_data.pop('poll_max_options', None),
                poll_duration=post_form.cleaned_data.pop('poll_duration', None),
                poll_user_changes=post_form.cleaned_data.pop('poll_user_changes', None),
            )

        return valid

    def form_invalid(self, post_form, attachment_formset, poll_option_formset, **kwargs):
        poll_errors = [k for k in post_form.errors.keys() if k.startswith('poll_')]
        if poll_errors or (poll_option_formset and not poll_option_formset.is_valid() and
                           len(post_form.cleaned_data['poll_question'])):
            messages.error(self.request, self.poll_option_formset_general_error_message)

        return super(BaseTopicFormView, self).form_invalid(
            post_form, attachment_formset, poll_option_formset=poll_option_formset, duplicate=self.duplicate, **kwargs)


class PostFormView(SingleObjectMixin, BasePostFormView):
    """
    A base view for manipulating post forms.
    """
    post_pk_url_kwarg = 'pk'
    topic_pk_url_kwarg = 'topic_pk'
    forum_pk_url_kwarg = 'forum_pk'


class TopicFormView(SingleObjectMixin, BaseTopicFormView):
    """
    A base view for manipulating topic forms.
    """
    topic_pk_url_kwarg = 'pk'
    forum_pk_url_kwarg = 'forum_pk'


class TopicCreateView(PermissionRequiredMixin, TopicFormView):
    """
    Allows users to create forum topics.
    """
    model = Topic
    template_name = 'forum_conversation/topic_create.html'
    permission_required = ['can_start_new_topics', ]

    def get(self, request, *args, **kwargs):
        self.object = None
        return super(TopicCreateView, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = None
        return super(TopicCreateView, self).post(request, *args, **kwargs)

    def get_success_url(self):
        if not self.forum_post.approved:
            return reverse('forum:forum', kwargs={
                'slug': self.forum_post.topic.forum.slug,
                'pk': self.forum_post.topic.forum.pk})
        return reverse('forum_conversation:topic', kwargs={
            'forum_slug': self.forum_post.topic.forum.slug,
            'forum_pk': self.forum_post.topic.forum.pk,
            'slug': self.forum_post.topic.slug,
            'pk': self.forum_post.topic.pk})

    # Permissions checks

    def get_controlled_object(self):
        return self.get_forum()


class TopicUpdateView(PermissionRequiredMixin, TopicFormView):
    """
    Allows users to update forum topics.
    """
    model = Topic
    template_name = 'forum_conversation/topic_update.html'
    success_message = _('This message has been edited successfully.')

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super(TopicUpdateView, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super(TopicUpdateView, self).post(request, *args, **kwargs)

    def get_object(self, queryset=None):
        return self.get_topic()

    def get_post(self):
        """
        Returns the considered post if applicable.
        """
        return self.get_topic().first_post

    def get_success_url(self):
        return reverse('forum_conversation:topic', kwargs={
            'forum_slug': self.forum_post.topic.forum.slug,
            'forum_pk': self.forum_post.topic.forum.pk,
            'slug': self.forum_post.topic.slug,
            'pk': self.forum_post.topic.pk})

    # Permissions checks

    def get_controlled_object(self):
        return self.get_topic().first_post

    def perform_permissions_check(self, user, obj, perms):
        return self.request.forum_permission_handler.can_edit_post(obj, user)


class PostCreateView(PermissionRequiredMixin, PostFormView):
    """
    Allows users to create forum posts.
    """
    model = Post
    template_name = 'forum_conversation/post_create.html'
    permission_required = ['can_reply_to_topics', ]

    def get(self, request, *args, **kwargs):
        self.object = None
        return super(PostCreateView, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = None
        return super(PostCreateView, self).post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(PostCreateView, self).get_context_data(**kwargs)
        topic = self.get_topic()

        # Add the previous posts to the context
        previous_posts = topic.posts.filter(approved=True) \
            .select_related('poster', 'updated_by') \
            .prefetch_related('attachments', 'poster__forum_profile') \
            .order_by('-created')
        previous_posts = previous_posts[:machina_settings.TOPIC_REVIEW_POSTS_NUMBER]
        context['previous_posts'] = previous_posts

        return context

    def get_success_url(self):
        return '{0}?post={1}#{1}'.format(
            reverse('forum_conversation:topic', kwargs={
                'forum_slug': self.forum_post.topic.forum.slug,
                'forum_pk': self.forum_post.topic.forum.pk,
                'slug': self.forum_post.topic.slug,
                'pk': self.forum_post.topic.pk}),
            self.forum_post.pk)

    # Permissions checks

    def get_controlled_object(self):
        return self.get_forum()


class PostUpdateView(PermissionRequiredMixin, PostFormView):
    """
    Allows users to update forum topics.
    """
    model = Post
    success_message = _('This message has been edited successfully.')
    template_name = 'forum_conversation/post_update.html'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super(PostUpdateView, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super(PostUpdateView, self).post(request, *args, **kwargs)

    def get_object(self, queryset=None):
        return self.get_post()

    def get_success_url(self):
        return '{0}?post={1}#{1}'.format(
            reverse('forum_conversation:topic', kwargs={
                'forum_slug': self.forum_post.topic.forum.slug,
                'forum_pk': self.forum_post.topic.forum.pk,
                'slug': self.forum_post.topic.slug,
                'pk': self.forum_post.topic.pk}),
            self.forum_post.pk)

    # Permissions checks

    def get_controlled_object(self):
        return self.get_post()

    def perform_permissions_check(self, user, obj, perms):
        return self.request.forum_permission_handler.can_edit_post(obj, user)


class PostDeleteView(PermissionRequiredMixin, DeleteView):
    """
    Allows users to delete forum topics.
    """
    template_name = 'forum_conversation/post_delete.html'
    success_message = _('This message has been deleted successfully.')
    model = Post

    def get_context_data(self, **kwargs):
        context = super(PostDeleteView, self).get_context_data(**kwargs)

        # Append the topic and the forum associated with the post being deleted
        # to the context
        post = self.get_object()
        context['topic'] = post.topic
        context['forum'] = post.topic.forum

        return context

    def get_success_url(self):
        messages.success(self.request, self.success_message)

        if self.object.is_topic_head and self.object.is_topic_tail:
            return reverse('forum:forum', kwargs={
                'slug': self.object.topic.forum.slug,
                'pk': self.object.topic.forum.pk})

        return reverse('forum_conversation:topic', kwargs={
            'forum_slug': self.object.topic.forum.slug,
            'forum_pk': self.object.topic.forum.pk,
            'slug': self.object.topic.slug,
            'pk': self.object.topic.pk})

    # Permissions checks

    def get_controlled_object(self):
        return self.get_object()

    def perform_permissions_check(self, user, obj, perms):
        return self.request.forum_permission_handler.can_delete_post(obj, user)

def PostUpvoteView(request, forum_slug, forum_pk, topic_slug, topic_pk, pk):
    post = get_object_or_404(Post, pk=pk)
    post.upvoters.add(request.user)
    post.vote_count += 1
    post.save()
    success_message = _('This message has been upvoted successfully.')
    success_url = '{0}?post={1}#{1}'.format(
            reverse('forum_conversation:topic', kwargs={
                'forum_slug': forum_slug,
                'forum_pk': forum_pk,
                'slug': topic_slug,
                'pk': topic_pk}), pk)
    messages.success(request, success_message)
    return HttpResponseRedirect(success_url)

def PostDownvoteView(request, forum_slug, forum_pk, topic_slug, topic_pk, pk):
    post = get_object_or_404(Post, pk=pk)
    post.upvoters.remove(request.user)
    post.vote_count -= 1
    post.save()    
    success_message = _('This message has been downvoted successfully.')
    success_url = '{0}?post={1}#{1}'.format(
            reverse('forum_conversation:topic', kwargs={
                'forum_slug': forum_slug,
                'forum_pk': forum_pk,
                'slug': topic_slug,
                'pk': topic_pk}), pk)
    messages.success(request, success_message)
    return HttpResponseRedirect(success_url)

def PostFlagView(request, forum_slug, forum_pk, topic_slug, topic_pk, pk):
    post = get_object_or_404(Post, pk=pk)
    post.flaggers.add(request.user)
    success_message = _('This message has been flagged successfully.')    
    post.flag_count += 1
    if post.flag_count > FLAG_THRESHOLD:
        post.approved = False
        success_message = _('This message has been flagged successfully and now under moderation.')
    post.save()
    success_url = '{0}?post={1}#{1}'.format(
            reverse('forum_conversation:topic', kwargs={
                'forum_slug': forum_slug,
                'forum_pk': forum_pk,
                'slug': topic_slug,
                'pk': topic_pk}), pk)
    messages.success(request, success_message)
    return HttpResponseRedirect(success_url)

def PostUnflagView(request, forum_slug, forum_pk, topic_slug, topic_pk, pk):
    post = get_object_or_404(Post, pk=pk)
    post.flaggers.remove(request.user)
    success_message = _('This message has been unflagged successfully.')
    post.flag_count -= 1
    if post.flag_count <= FLAG_THRESHOLD:
        post.approved = True
        success_message = _('This message has been unflagged successfully and now approved.')    
    post.save()    
    success_url = '{0}?post={1}#{1}'.format(
            reverse('forum_conversation:topic', kwargs={
                'forum_slug': forum_slug,
                'forum_pk': forum_pk,
                'slug': topic_slug,
                'pk': topic_pk}), pk)
    messages.success(request, success_message)
    return HttpResponseRedirect(success_url)    

