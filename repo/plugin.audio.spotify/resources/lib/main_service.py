"""
    plugin.audio.spotify
    Spotify player for Kodi
    main_service.py
    Background service which launches the spotty binary and monitors the player.
"""

import time
from typing import Dict

import xbmc
import xbmcaddon
import xbmcgui
from xbmc import LOGDEBUG, LOGWARNING

import bottle_manager
import utils
from http_spotty_audio_streamer import HTTPSpottyAudioStreamer
from http_video_player_setter import HttpVideoPlayerSetter
from save_recently_played import SaveRecentlyPlayed
from spotty import Spotty
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import HTTP_VIDEO_RULE_ADDED_STR_ID
from utils import PROXY_PORT, log_msg, ADDON_ID

SAVE_TO_RECENTLY_PLAYED_FILE = True

SPOTIFY_ADDON = xbmcaddon.Addon(id=ADDON_ID)


def abort_app(timeout_in_secs: int) -> bool:
    return xbmc.Monitor().waitForAbort(timeout_in_secs)


def add_http_video_rule() -> None:
    video_player_setter = HttpVideoPlayerSetter()

    if not video_player_setter.set_http_rule():
        return

    msg = SPOTIFY_ADDON.getLocalizedString(HTTP_VIDEO_RULE_ADDED_STR_ID)
    dialog = xbmcgui.Dialog()
    header = SPOTIFY_ADDON.getAddonInfo("name")
    dialog.ok(header, msg)


class MainService:
    def __init__(self):
        log_msg(f"Spotify plugin version: {xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('version')}.")

        self.__spotty_helper: SpottyHelper = SpottyHelper()

        self.__spotty = Spotty()
        self.__spotty.set_spotty_paths(
            self.__spotty_helper.spotty_binary_path, self.__spotty_helper.spotty_cache_path
        )
        self.__spotty.set_spotty_env(self.__spotty_helper.spotty_rust_env)

        self.__spotty_auth: SpottyAuth = SpottyAuth(self.__spotty)
        self.__auth_token: Dict[str, str] = dict()

        # Workaround to make Kodi use it's VideoPlayer to play http audio streams.
        # If we don't do this, then Kodi uses PAPlayer which does not stream.
        add_http_video_rule()

        gap_between_tracks = int(SPOTIFY_ADDON.getSetting("gap_between_playlist_tracks"))
        use_spotify_normalization = SPOTIFY_ADDON.getSetting("use_spotify_normalization").lower() == "true"
        self.__http_spotty_streamer: HTTPSpottyAudioStreamer = HTTPSpottyAudioStreamer(
            self.__spotty, gap_between_tracks, use_spotify_normalization
        )
        self.__save_recently_played: SaveRecentlyPlayed = SaveRecentlyPlayed()
        self.__http_spotty_streamer.set_notify_track_finished(self.__save_track_to_recently_played)

        bottle_manager.route_all(self.__http_spotty_streamer)

    def __save_track_to_recently_played(self, track_id: str) -> None:
        if SAVE_TO_RECENTLY_PLAYED_FILE:
            self.__save_recently_played.save_track(track_id)

    def run(self) -> None:
        log_msg("Starting main service loop.")

        bottle_manager.start_thread(PROXY_PORT)
        log_msg(f"Started bottle with port {PROXY_PORT}.")

        self.__renew_token()

        loop_counter = 0
        loop_wait_in_secs = 6
        while True:
            loop_counter += 1
            if (loop_counter % 10) == 0:
                log_msg(f"Main loop continuing. Loop counter: {loop_counter}.")

            self.__http_spotty_streamer.use_normalization(
                SPOTIFY_ADDON.getSetting("use_spotify_normalization").lower() == "true"
            )

            # Monitor authorization.
            if (int(self.__auth_token["expires_at"]) - 60) <= (int(time.time())):
                expire_time = int(self.__auth_token["expires_at"])
                time_now = int(time.time())
                log_msg(
                    f"Spotify token expired."
                    f" Expire time: {self.__get_time_str(expire_time)} ({expire_time});"
                    f" time now: {self.__get_time_str(time_now)} ({time_now})."
                )
                log_msg("Refreshing auth token now.")
                self.__renew_token()

            if abort_app(loop_wait_in_secs):
                break

        self.__close()

    def __close(self) -> None:
        log_msg("Shutdown requested.")
        self.__http_spotty_streamer.stop()
        self.__spotty_helper.kill_all_spotties()
        bottle_manager.stop_thread()
        log_msg("Main service stopped.")

    def __renew_token(self) -> None:
        log_msg("Retrieving auth token....", LOGDEBUG)

        self.__auth_token = self.__get_retry_auth_token()
        if not self.__auth_token:
            utils.cache_auth_token("")
            raise Exception(
                f"Could not get Spotify auth token for"
                f" user '{self.__spotty_helper.get_username()}'."
            )

        log_msg(
            f"Retrieved Spotify auth token."
            f" Expires at {self.__get_time_str(int(self.__auth_token['expires_at']))}."
        )

        # Cache auth token for easy access by the plugin.
        utils.cache_auth_token(self.__auth_token["access_token"])

    def __get_retry_auth_token(self) -> Dict[str, str]:
        auth_token = None
        max_retries = 20
        count = 0
        while count < max_retries:
            auth_token = self.__get_token()
            if auth_token:
                break
            time.sleep(1)
            count += 1

        if count > 0:
            log_msg(f"Took {count} retries to get authorization token.", LOGWARNING)

        return auth_token

    def __get_token(self) -> Dict[str, str]:
        self.__spotty.set_spotify_user(
            self.__spotty_helper.get_username(), self.__spotty_helper.get_password()
        )
        return self.__spotty_auth.get_token()

    @staticmethod
    def __get_time_str(raw_time: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(raw_time)))
