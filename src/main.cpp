#include <Geode/Geode.hpp>
#include <Geode/modify/PlayLayer.hpp>
#include <Geode/utils/web.hpp>
#include <Geode/utils/string.hpp>
#include <Geode/utils/file.hpp>
#include <matjson.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <optional>
#include <string>

#include "gdquiet_config.hpp"

using namespace geode::prelude;

namespace {
    struct ReportPayload {
        std::string accountNumber;
        double percent = 0.0;
        bool isPlaying = false;
        bool shouldMute = false;
        std::string levelName;
    };

    std::string normalizeUrl(std::string url) {
        url = utils::string::trim(url);
        while (!url.empty() && url.back() == '/') {
            url.pop_back();
        }
        return url;
    }

    class GDQuietClient final {
    public:
        static GDQuietClient& get() {
            static GDQuietClient instance;
            return instance;
        }

        void loadConfig() {
            m_supabaseUrl = normalizeUrl(GDQUIET_BAKED_SUPABASE_URL);
            m_supabaseAnonKey = utils::string::trim(GDQUIET_BAKED_SUPABASE_ANON_KEY);

            // Optional local override for development/testing before baking secrets into CI.
            auto overridePath = Mod::get()->getSaveDir() / "supabase.json";
            std::error_code ec;
            if (std::filesystem::exists(overridePath, ec) && !ec) {
                auto jsonRes = utils::file::readJson(overridePath);
                if (jsonRes.isOk()) {
                    auto const& obj = jsonRes.unwrap();
                    if (obj.contains("supabase_url")) {
                        if (auto urlRes = obj["supabase_url"].asString(); urlRes.isOk()) {
                            auto candidate = normalizeUrl(urlRes.unwrap());
                            if (!candidate.empty()) {
                                m_supabaseUrl = candidate;
                            }
                        }
                    }
                    if (obj.contains("supabase_anon_key")) {
                        if (auto keyRes = obj["supabase_anon_key"].asString(); keyRes.isOk()) {
                            auto candidate = utils::string::trim(keyRes.unwrap());
                            if (!candidate.empty()) {
                                m_supabaseAnonKey = candidate;
                            }
                        }
                    }
                }
            }

            if (!isConfigured()) {
                log::warn("GD Quiet: Supabase URL/anon key is not configured yet.");
            }
        }

        bool isConfigured() const {
            return !m_supabaseUrl.empty() && !m_supabaseAnonKey.empty();
        }

        double effectiveThreshold() const {
            double fallback = Mod::get()->getSettingValue<double>("mute-at-percent");
            return std::clamp(m_syncedMutePercent.value_or(fallback), 0.0, 100.0);
        }

        void clearSyncedState() {
            m_syncedMutePercent.reset();
            m_lastSyncedAccount.clear();
        }

        void syncAccount(std::string const& accountNumber, bool force = false) {
            auto code = utils::string::trim(accountNumber);
            if (code.empty() || !isConfigured()) {
                return;
            }
            if (!force && m_lastSyncedAccount == code && m_syncedMutePercent.has_value()) {
                return;
            }
            m_lastSyncedAccount = code;

            auto body = matjson::makeObject({
                { "p_account_number", code }
            });

            auto req = web::WebRequest()
                .header("apikey", m_supabaseAnonKey)
                .header("Content-Type", "application/json")
                .timeout(std::chrono::seconds(8))
                .bodyJSON(body);

            m_syncListener.bind([this, code](web::WebTask::Event* e) {
                if (auto* res = e->getValue()) {
                    if (!res->ok()) {
                        return;
                    }
                    auto jsonRes = res->json();
                    if (!jsonRes.isOk()) {
                        return;
                    }
                    auto const& data = jsonRes.unwrap();
                    if (data.contains("found")) {
                        auto foundRes = data["found"].asBool();
                        if (!foundRes.isOk() || !foundRes.unwrap()) {
                            return;
                        }
                    }
                    if (data.contains("mute_at_percent")) {
                        if (auto pctRes = data["mute_at_percent"].asDouble(); pctRes.isOk()) {
                            m_syncedMutePercent = std::clamp(pctRes.unwrap(), 0.0, 100.0);
                        }
                    }
                }
            });

            m_syncListener.setFilter(
                req.post(fmt::format("{}/rest/v1/rpc/gdquiet_mod_sync", m_supabaseUrl))
            );
        }

        void reportState(
            std::string const& accountNumber,
            double percent,
            bool isPlaying,
            bool shouldMute,
            std::string const& levelName
        ) {
            auto code = utils::string::trim(accountNumber);
            if (code.empty() || !isConfigured()) {
                return;
            }

            ReportPayload payload{
                .accountNumber = code,
                .percent = std::clamp(percent, 0.0, 100.0),
                .isPlaying = isPlaying,
                .shouldMute = shouldMute,
                .levelName = levelName
            };

            if (m_reportInFlight) {
                // Keep only the newest state so exit/unmute transitions are never dropped.
                m_pendingReport = std::move(payload);
                return;
            }

            dispatchReport(payload);
        }

    private:
        GDQuietClient() {
            m_reportListener.bind([this](web::WebTask::Event* e) {
                if (auto* res = e->getValue()) {
                    if (res->ok()) {
                        auto jsonRes = res->json();
                        if (jsonRes.isOk()) {
                            auto const& data = jsonRes.unwrap();
                            if (data.contains("mute_at_percent")) {
                                if (auto pctRes = data["mute_at_percent"].asDouble(); pctRes.isOk()) {
                                    m_syncedMutePercent = std::clamp(pctRes.unwrap(), 0.0, 100.0);
                                }
                            }
                        }
                    }
                    m_reportInFlight = false;
                    flushPendingReport();
                } else if (e->isCancelled()) {
                    m_reportInFlight = false;
                    flushPendingReport();
                }
            });
        }

        void flushPendingReport() {
            if (m_pendingReport.has_value() && !m_reportInFlight) {
                auto next = std::move(*m_pendingReport);
                m_pendingReport.reset();
                dispatchReport(next);
            }
        }

        void dispatchReport(ReportPayload const& payload) {
            if (!isConfigured()) {
                return;
            }

            m_reportInFlight = true;

            auto body = matjson::makeObject({
                { "p_account_number", payload.accountNumber },
                { "p_percent", payload.percent },
                { "p_is_playing", payload.isPlaying },
                { "p_should_mute", payload.shouldMute },
                { "p_level_name", payload.levelName }
            });

            auto req = web::WebRequest()
                .header("apikey", m_supabaseAnonKey)
                .header("Content-Type", "application/json")
                .timeout(std::chrono::seconds(8))
                .bodyJSON(body);

            m_reportListener.setFilter(
                req.post(fmt::format("{}/rest/v1/rpc/gdquiet_mod_report", m_supabaseUrl))
            );
        }

        std::string m_supabaseUrl;
        std::string m_supabaseAnonKey;
        std::string m_lastSyncedAccount;
        std::optional<double> m_syncedMutePercent;

        EventListener<web::WebTask> m_syncListener;
        EventListener<web::WebTask> m_reportListener;
        bool m_reportInFlight = false;
        std::optional<ReportPayload> m_pendingReport;
    };

    std::string activeAccountNumber() {
        return utils::string::trim(Mod::get()->getSettingValue<std::string>("account-number"));
    }

    bool isModEnabled() {
        return Mod::get()->getSettingValue<bool>("enabled");
    }
}

$on_mod(Loaded) {
    GDQuietClient::get().loadConfig();

    listenForSettingChangesV3<std::string>("account-number", [](std::string value) {
        GDQuietClient::get().clearSyncedState();
        auto trimmed = utils::string::trim(value);
        if (!trimmed.empty() && isModEnabled()) {
            GDQuietClient::get().syncAccount(trimmed, true);
        }
    });

    listenForSettingChangesV3<bool>("enabled", [](bool enabled) {
        if (!enabled) {
            auto account = activeAccountNumber();
            if (!account.empty()) {
                GDQuietClient::get().reportState(account, 0.0, false, false, "");
            }
        }
    });
}

class $modify(QuietPlayLayer, PlayLayer) {
    struct Fields {
        bool active = false;
        bool muted = false;
        double lastSentPercent = -1.0;
        float reportTimer = 0.0f;
        float heartbeatTimer = 0.0f;
        std::string levelName;
    };

    bool init(GJGameLevel* level, bool useReplay, bool dontCreateObjects) {
        if (!PlayLayer::init(level, useReplay, dontCreateObjects)) {
            return false;
        }

        m_fields->active = true;
        m_fields->muted = false;
        m_fields->lastSentPercent = 0.0;
        m_fields->reportTimer = 0.0f;
        m_fields->heartbeatTimer = 0.0f;
        m_fields->levelName = level ? std::string(level->m_levelName) : "";

        auto account = activeAccountNumber();
        if (isModEnabled() && !account.empty()) {
            GDQuietClient::get().syncAccount(account, true);
            GDQuietClient::get().reportState(account, 0.0, true, false, m_fields->levelName);
        }

        return true;
    }

    void postUpdate(float dt) {
        PlayLayer::postUpdate(dt);
        if (!m_fields->active) {
            return;
        }

        auto account = activeAccountNumber();
        if (!isModEnabled() || account.empty()) {
            if (m_fields->muted) {
                m_fields->muted = false;
                if (!account.empty()) {
                    GDQuietClient::get().reportState(account, 0.0, false, false, "");
                }
            }
            return;
        }

        double pct = std::clamp(static_cast<double>(this->getCurrentPercent()), 0.0, 100.0);
        bool deadOrEnded = (m_player1 && m_player1->m_isDead)
            || m_hasCompletedLevel
            || m_levelEndAnimationStarted;
        double threshold = GDQuietClient::get().effectiveThreshold();
        bool wantMute = !deadOrEnded && (pct >= threshold);

        m_fields->reportTimer += dt;
        m_fields->heartbeatTimer += dt;

        bool stateChanged = (wantMute != m_fields->muted);
        bool percentChanged = (m_fields->reportTimer >= 0.75f && std::abs(pct - m_fields->lastSentPercent) >= 1.0);
        bool keepAlive = (m_fields->heartbeatTimer >= 4.0f);

        if (stateChanged || percentChanged || keepAlive) {
            m_fields->muted = wantMute;
            m_fields->lastSentPercent = pct;
            m_fields->reportTimer = 0.0f;
            m_fields->heartbeatTimer = 0.0f;
            GDQuietClient::get().reportState(account, pct, true, wantMute, m_fields->levelName);
        }
    }

    void resetLevel() {
        PlayLayer::resetLevel();
        if (!m_fields->active) {
            return;
        }

        auto account = activeAccountNumber();
        if (!isModEnabled() || account.empty()) {
            return;
        }

        double pct = std::clamp(static_cast<double>(this->getCurrentPercent()), 0.0, 100.0);
        double threshold = GDQuietClient::get().effectiveThreshold();
        bool wantMute = pct >= threshold;
        if (m_fields->muted && !wantMute) {
            m_fields->muted = false;
            m_fields->lastSentPercent = pct;
            m_fields->reportTimer = 0.0f;
            m_fields->heartbeatTimer = 0.0f;
            GDQuietClient::get().reportState(account, pct, true, false, m_fields->levelName);
        }
    }

    void levelComplete() {
        PlayLayer::levelComplete();
        if (!m_fields->active) {
            return;
        }

        auto account = activeAccountNumber();
        if (m_fields->muted && !account.empty()) {
            m_fields->muted = false;
            m_fields->lastSentPercent = 100.0;
            m_fields->reportTimer = 0.0f;
            m_fields->heartbeatTimer = 0.0f;
            GDQuietClient::get().reportState(account, 100.0, true, false, m_fields->levelName);
        }
    }

    void onExit() {
        m_fields->active = false;
        m_fields->muted = false;
        auto account = activeAccountNumber();
        if (!account.empty()) {
            GDQuietClient::get().reportState(account, 0.0, false, false, "");
        }
        PlayLayer::onExit();
    }
};
