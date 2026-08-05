from django.urls import path
from . import views
from . import ai_worker_api
from . import mailbox_api

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', views.manager_login, name='login'),
    path('logout/', views.manager_logout, name='logout'),
    path('register/', views.register, name='register'),
    path('hub/', views.dashboard, name='hub_dashboard'),
    path('hub/mosaic/', views.mosaic_monitor_settings, name='mosaic_monitor_settings'),
    path('hub/mailboxes/', views.mailbox_list, name='mailbox_list'),
    path('hub/api/mailboxes/search/', views.mailbox_search_api, name='mailbox_search_api'),
    path('hub/mailboxes/add/', views.mailbox_add, name='mailbox_add'),
    path('hub/mailboxes/<int:pk>/edit/', views.mailbox_edit, name='mailbox_edit'),
    path('hub/mailboxes/<int:pk>/tracking/', views.mailbox_toggle_tracking, name='mailbox_toggle_tracking'),
    path('hub/mailboxes/sync/', views.mailbox_sync_all, name='mailbox_sync_all'),
    path('hub/mailboxes/<int:pk>/sync/', views.mailbox_sync, name='mailbox_sync'),
    path('hub/inbox/', views.inbox, name='inbox'),
    path('hub/ai/', views.ai_dashboard, name='ai_dashboard'),
    path('hub/ai/messages/<int:pk>/analyze/', views.queue_message_ai_analysis, name='queue_message_ai_analysis'),
    path('hub/inbox/bulk/', views.inbox_bulk, name='inbox_bulk'),
    path('hub/inbox/<int:pk>/', views.message_detail, name='message_detail'),
    path('hub/inbox/<int:pk>/html/', views.message_html, name='message_html'),
    path('hub/inbox/attachments/<int:pk>/download/', views.inbound_attachment_download, name='inbound_attachment_download'),
    path('hub/compose/', views.compose, name='compose'),
    path('hub/team/', views.shared_mail, name='shared_mail'),
    path('hub/team/bulk/', views.shared_mail_bulk, name='shared_mail_bulk'),
    path('hub/team/mailboxes/<int:pk>/sync/', views.shared_mailbox_sync, name='shared_mailbox_sync'),
    path('hub/team/compose/', views.shared_compose, name='shared_compose'),
    path('hub/team/inbox/<int:pk>/read/', views.shared_message_mark_read, name='shared_message_mark_read'),
    path('hub/team/inbox/<int:pk>/', views.shared_message_detail, name='shared_message_detail'),
    path('hub/campaigns/', views.campaign_center, name='campaign_center'),
    path('api/ai-worker/lease/', ai_worker_api.lease_ai_job, name='ai_worker_lease'),
    path('api/ai-worker/submit/', ai_worker_api.submit_ai_job, name='ai_worker_submit'),
    path('api/v1/mailboxes/', mailbox_api.create_mailbox, name='api_create_mailbox'),
]
