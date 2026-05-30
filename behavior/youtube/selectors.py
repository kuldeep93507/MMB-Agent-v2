"""
Semantic selector maps for desktop (ytd-) and mobile (ytm-) YouTube UIs.

Prefer aria-label and visible text over brittle element IDs.
"""

from __future__ import annotations

from typing import TypedDict


class SemanticSpec(TypedDict, total=False):
    css: tuple[str, ...]
    xpath: tuple[str, ...]
    aria_labels: tuple[str, ...]
    text: tuple[str, ...]


DESKTOP_SELECTORS: dict[str, SemanticSpec] = {
    "search_bar": {
        "css": (
            'input#search',
            'input[name="search_query"]',
            '#search-icon-legacy',
            'ytd-searchbox input#search',
            'input[aria-label*="Search"]',
            'input[placeholder*="Search"]',
        ),
        "xpath": (
            '//input[@id="search"]',
            '//ytd-searchbox//input[@type="text"]',
        ),
        "aria_labels": ("Search",),
    },
    "search_button": {
        "css": (
            '#search-icon-legacy',
            'button#search-icon-legacy',
            'button[aria-label="Search"]',
        ),
        "xpath": ('//button[@id="search-icon-legacy"]',),
        "aria_labels": ("Search",),
    },
    "consent_accept": {
        "css": (
            'button[aria-label*="Accept"]',
            'button[aria-label*="Agree"]',
        ),
        "text": ("Accept all", "I agree", "Accept"),
        "aria_labels": ("Accept all", "Accept the use of cookies"),
    },
    "video_link": {
        "css": (
            'ytd-video-renderer a#video-title',
            'a#video-title',
            'a#video-title-link',
            'ytd-video-renderer a#thumbnail',
            'ytd-grid-video-renderer a#thumbnail',
            'a.ytd-thumbnail[href*="/watch"]',
        ),
        "xpath": ('//a[contains(@href, "/watch?v=")]',),
    },
    "suggested_video": {
        "css": (
            'ytd-compact-video-renderer a#thumbnail',
            '#related a#thumbnail',
            'ytd-watch-next-secondary-results-renderer a#thumbnail',
        ),
        "xpath": ('//div[@id="related"]//a[contains(@href, "/watch?v=")]',),
    },
    "homepage_feed_video": {
        "css": (
            'ytd-rich-item-renderer a#thumbnail',
            'ytd-rich-grid-media a#thumbnail',
            'ytd-video-renderer a#thumbnail',
        ),
        "xpath": ('//ytd-rich-item-renderer//a[@id="thumbnail"]',),
    },
    "like_button": {
        "css": (
            'like-button-view-model button',
            'ytd-toggle-button-renderer#top-level-buttons-computed button:first-child',
            'button[aria-label*="like" i]:not([aria-label*="dislike" i])',
            'button[aria-label*="like this video"]',
            'button[aria-label*="Like this video"]',
        ),
        "xpath": ('//button[contains(@aria-label, "like this video")]',),
        "aria_labels": ("like this video", "Like this video"),
    },
    "subscribe_button": {
        "css": (
            '#subscribe-button button',
            'ytd-subscribe-button-renderer button',
            'button[aria-label*="Subscribe"]:not([aria-label*="Unsubscribe" i])',
        ),
        "xpath": ('//button[contains(@aria-label, "Subscribe")]',),
        "aria_labels": ("Subscribe",),
        "text": ("Subscribe",),
    },
    "player": {
        "css": (
            ".html5-video-player",
            "#movie_player",
            "video.html5-main-video",
        ),
    },
    "settings_button": {
        "css": (
            '.ytp-settings-button',
            'button.ytp-settings-button',
            'button[aria-label="Settings"]',
        ),
        "aria_labels": ("Settings",),
    },
    "playback_speed_menu": {
        "text": ("Playback speed", "Playback speed"),
        "css": ('.ytp-menuitem-label',),
    },
    "comments_section": {
        "css": ('#comments', 'ytd-comments#comments'),
        "xpath": ('//ytd-comments',),
        "text": ("Comments",),
    },
    "comment_box": {
        "css": ('#placeholder-area', '#simplebox-placeholder'),
        "text": ("Add a comment...",),
    },
    "comment_input": {
        "css": ('#contenteditable-root',),
    },
    "comment_submit": {
        "css": ('#submit-button button',),
        "aria_labels": ("Comment",),
    },
    "search_results": {
        "css": (
            'ytd-video-renderer',
            'ytd-item-section-renderer ytd-video-renderer',
        ),
    },
    "bell_button": {
        "css": (
            'ytd-subscribe-button-renderer ytd-notification-toggle-button-renderer button',
            'button[aria-label*="Notification" i]',
            'button[aria-label*="notification bell" i]',
        ),
        "aria_labels": ("Notify me", "Turn on notifications", "Notifications"),
    },
    "autoplay_toggle": {
        "text": ("Autoplay",),
        "css": ('.ytp-autonav-toggle-button', 'button.ytp-autonav-toggle-button'),
    },
    "quality_menu": {
        "text": ("Quality",),
        "css": ('.ytp-menuitem-label',),
    },
    # Ad detection + skip
    "ad_overlay": {
        "css": (
            '.ad-interrupting',
            '.ytp-ad-player-overlay',
            '.ytp-ad-player-overlay-instream-info',
            '.ytp-ad-module',
            '.ytp-ad-badge',
            '.ytp-ad-text',
            '.video-ads.ytp-ad-module',
            '[class*="ad-showing"]',
        ),
    },
    "ad_skip_button": {
        "css": (
            '.ytp-skip-ad-button',
            '.ytp-ad-skip-button',
            '.ytp-ad-skip-button-modern',
            'button.ytp-skip-ad-button',
            '[class*="skip-ad"]',
        ),
        "aria_labels": ("Skip Ad", "Skip Ads", "Skip ad"),
    },
    "dislike_button": {
        "css": (
            'dislike-button-view-model button',
            '#segmented-dislike-button button',
            'button[aria-label*="dislike" i]:not([aria-label*="unlike" i])',
            'button[aria-label*="Dislike this video"]',
        ),
        "aria_labels": ("Dislike this video", "dislike this video"),
    },
    "share_button": {
        "css": (
            'button[aria-label="Share"]',
            '.yt-spec-button-shape-next[aria-label="Share"]',
            'ytd-button-renderer[button-renderer] button[aria-label="Share"]',
        ),
        "aria_labels": ("Share",),
        "text": ("Share",),
    },
    "save_playlist_button": {
        "css": (
            'button[aria-label*="Save"]',
            'button[aria-label*="Save to playlist"]',
            '.yt-spec-button-shape-next[aria-label*="Save"]',
        ),
        "aria_labels": ("Save to playlist", "Save"),
        "text": ("Save",),
    },
}

MOBILE_SELECTORS: dict[str, SemanticSpec] = {
    "search_bar": {
        "css": (
            'input[type="search"]',
            'form[action="/results"] input',
            'ytm-searchbox input',
            'input[name="search_query"]',
            '#search-input input',
        ),
        "xpath": (
            '//ytm-searchbox//input',
            '//input[@type="search"]',
        ),
        "aria_labels": ("Search YouTube", "Search"),
    },
    "search_button": {
        "css": (
            'button[aria-label="Search"]',
            'ytm-searchbox button',
            'button.search-button',
        ),
        "aria_labels": ("Search",),
    },
    "search_open": {
        "css": (
            'button[aria-label*="Search" i]',
            'button[aria-label="Search YouTube"]',
            'button[aria-label="Search"]',
            'a[aria-label="Search"]',
            'ytm-topbar-menu-button-renderer button',
            '.mobile-topbar-header-content button',
            'ytm-searchbox button',
            'header button[aria-label*="Search"]',
        ),
        "xpath": (
            '//button[contains(@aria-label, "Search")]',
            '//a[contains(@aria-label, "Search")]',
            '//button[.//*[local-name()="svg"]]',
            '//ytm-searchbox//button',
        ),
        "aria_labels": ("Search YouTube", "Search"),
        "text": ("Search",),
    },
    "search_input_visible": {
        "css": (
            'input[type="search"]',
            'input[name="search_query"]',
            'input[placeholder*="Search" i]',
            'input[aria-label*="Search" i]',
            'ytm-searchbox input',
            '.searchbox-input input',
            'form[action="/results"] input',
            '#search-input input',
        ),
        "xpath": (
            '//ytm-searchbox//input',
            '//input[@type="search"]',
            '//input[contains(@placeholder, "Search")]',
        ),
        "aria_labels": ("Search YouTube", "Search"),
    },
    "consent_accept": {
        "text": ("Accept all", "I agree", "Accept"),
        "css": ('button[aria-label*="Accept"]',),
    },
    "video_link": {
        "css": (
            'ytm-video-with-context-renderer',
            'ytm-compact-video-renderer',
            'ytm-item-section-renderer ytm-video-with-context-renderer',
            'a.media-item-thumbnail-container',
            'ytm-compact-video-renderer a',
            'a[href*="/watch?v="]',
        ),
        "xpath": (
            '//ytm-video-with-context-renderer',
            '//ytm-compact-video-renderer',
            '//a[contains(@href, "/watch?v=")]',
        ),
    },
    "suggested_video": {
        "css": (
            'ytm-compact-video-renderer a',
            'ytm-item-section-renderer a[href*="/watch"]',
            '.related-chips-slot ~ ytm-compact-video-renderer a',
        ),
        "xpath": ('//ytm-compact-video-renderer//a[contains(@href, "/watch")]',),
    },
    "homepage_feed_video": {
        "css": (
            'ytm-rich-item-renderer a',
            'ytm-video-with-context-renderer a',
            'a.media-item-thumbnail-container',
        ),
        "xpath": ('//ytm-rich-item-renderer//a[contains(@href, "/watch")]',),
    },
    "like_button": {
        "css": (
            'ytm-like-button-renderer button[aria-label*="like" i]:not([aria-label*="dislike" i])',
            'button[aria-label*="like" i]:not([aria-label*="dislike" i])',
            'ytm-toggle-button-renderer button',
        ),
        "aria_labels": ("like this video", "Like this video", "Like"),
    },
    "subscribe_button": {
        "css": (
            'ytm-subscribe-button-renderer button',
            '.yt-spec-button-shape-next[aria-label*="Subscribe" i]',
            'button[aria-label*="Subscribe" i]:not([aria-label*="Unsubscribe" i])',
        ),
        "aria_labels": ("Subscribe",),
        "text": ("Subscribe",),
    },
    "player": {
        "css": (
            '.html5-video-player',
            'video',
            '.player-container',
        ),
    },
    "settings_button": {
        "css": (
            'button[aria-label="Settings"]',
            'button[aria-label="More actions"]',
            '.player-controls button[aria-label*="Settings"]',
        ),
        "aria_labels": ("Settings", "More actions"),
    },
    "playback_speed_menu": {
        "text": ("Playback speed",),
    },
    "comments_section": {
        "css": ('ytm-comments-entry-point-header-renderer',),
        "text": ("Comments",),
    },
    "comment_box": {
        "text": ("Add a comment...",),
    },
    "comment_input": {
        "css": ('textarea', 'input[type="text"]', '[contenteditable="true"]'),
    },
    "comment_submit": {
        "aria_labels": ("Comment",),
        "text": ("Comment",),
    },
    "search_results": {
        "css": (
            'ytm-video-with-context-renderer',
            'ytm-compact-video-renderer',
        ),
    },
    "bell_button": {
        "css": (
            'ytm-subscribe-button-renderer button[aria-label*="Notification" i]',
            'button[aria-label*="Notify" i]',
        ),
        "aria_labels": ("Notify me", "Turn on notifications"),
    },
}
