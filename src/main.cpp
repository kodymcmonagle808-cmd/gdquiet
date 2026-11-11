#include <Geode/Geode.hpp>
#include <Geode/modify/PlayLayer.hpp>
#include <Geode/utils/web.hpp>

using namespace geode::prelude;

class $modify(MutePlayLayer, PlayLayer) {
    struct Fields {
        bool muted = false;
        EventListener<web::WebTask> listener;
    };

    void sendState(bool mute) {
        auto req = web::WebRequest();
        m_fields->listener.bind([](web::WebTask::Event* e) {});
        m_fields->listener.setFilter(
            req.post(mute ? "http://127.0.0.1:8765/mute" : "http://127.0.0.1:8765/unmute")
        );
    }

    void postUpdate(float dt) {
        PlayLayer::postUpdate(dt);
        double threshold = Mod::get()->getSettingValue<double>("mute-at-percent");
        bool want = this->getCurrentPercent() >= threshold;
        if (want != m_fields->muted) {
            m_fields->muted = want;
            sendState(want);
        }
    }

    void onExit() {
        if (m_fields->muted) {
            m_fields->muted = false;
            sendState(false);
        }
        PlayLayer::onExit();
    }
};
