#include <iostream>
#include <cassert>
#include <memory>
#include <dcp/helper/Helper.hpp>
#include <dcp/helper/LogHelper.hpp>
#include <dcp/model/LogEntry.hpp>
#include <dcp/model/constant/DcpDataType.hpp>
#include <dcp/model/constant/DcpError.hpp>
#include <dcp/model/constant/DcpLogLevel.hpp>
#include <dcp/model/constant/DcpLogMode.hpp>
#include <dcp/model/constant/DcpOpMode.hpp>
#include <dcp/model/constant/DcpPduType.hpp>
#include <dcp/model/constant/DcpScope.hpp>
#include <dcp/model/constant/DcpState.hpp>
#include <dcp/model/constant/DcpTransportProtocol.hpp>
#include <dcp/model/pdu/DcpPduFactory.hpp>
#include <dcp/logic/DcpManagerMaster.hpp>
#include <dcp/driver/ethernet/udp/UdpDriver.hpp>

#include <asio.hpp>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
constexpr const char* HOST = "127.0.0.1";
constexpr std::uint16_t MASTER_CONTROL_PORT = 55200;
constexpr std::uint16_t CONTROLLER_CONTROL_PORT = 55201;
constexpr std::uint16_t TANK_CONTROL_PORT = 55202;
constexpr std::uint16_t CONTROLLER_DATA_INPUT_PORT = 55101;
constexpr std::uint16_t TANK_DATA_INPUT_PORT = 55102;
constexpr std::uint32_t COMMUNICATION_STEP_SECONDS = 1;

constexpr std::uint8_t CONTROLLER_ID = 1;
constexpr std::uint8_t TANK_ID = 2;
constexpr const char* CONTROLLER_UUID = "8e77346c-6274-4f01-86ed-0f077b4d2511";
constexpr const char* TANK_UUID = "1142a519-4294-4b7d-a0e8-80fd4b6b7d1c";

constexpr std::uint64_t CONTROLLER_Y_VR = 1;
constexpr std::uint64_t CONTROLLER_U_VR = 2;
constexpr std::uint64_t CONTROLLER_R_VR = 3;
constexpr std::uint64_t CONTROLLER_KP_VR = 4;
constexpr std::uint64_t CONTROLLER_BIAS_VR = 5;

constexpr std::uint64_t TANK_U_VR = 1;
constexpr std::uint64_t TANK_H_VR = 2;
constexpr std::uint64_t TANK_AREA_VR = 3;
constexpr std::uint64_t TANK_K_VR = 4;
constexpr std::uint64_t TANK_H0_VR = 5;

class DcpMasterDemo {
public:
    DcpMasterDemo(std::uint32_t durationSeconds, std::uint32_t delaySteps, double reference)
        : durationSeconds_(durationSeconds), delaySteps_(delaySteps),
          communicationIntervals_(durationSeconds), reference_(reference) {
        driver_ = std::make_unique<UdpDriver>(HOST, MASTER_CONTROL_PORT);
        manager_ = std::make_unique<DcpManagerMaster>(driver_->getDcpDriver());
        setSlaveEndpoint(CONTROLLER_ID, CONTROLLER_CONTROL_PORT);
        setSlaveEndpoint(TANK_ID, TANK_CONTROL_PORT);
        expectedAcks_[CONTROLLER_ID] = 11;
        expectedAcks_[TANK_ID] = 11;

        manager_->setAckReceivedListener<SYNC>([this](std::uint8_t sender, std::uint16_t) {
            ++receivedAcks_[sender];
            if (receivedAcks_[sender] == expectedAcks_[sender]) {
                manager_->STC_prepare(sender, DcpState::CONFIGURATION);
            }
        });
        manager_->setNAckReceivedListener<SYNC>([this](std::uint8_t sender, std::uint16_t, DcpError error) {
            std::cerr << "DCP NACK from slave " << static_cast<int>(sender)
                      << ", error=" << static_cast<int>(error) << std::endl;
            fail("A DCP slave rejected the configuration.");
        });
        manager_->setStateChangedNotificationReceivedListener<SYNC>(
            [this](std::uint8_t sender, DcpState state) { onStateChanged(sender, state); });
    }

    int run() {
        std::cout << "DCP master: NRT simulation on localhost, duration=" << durationSeconds_
                  << " s, H=" << COMMUNICATION_STEP_SECONDS << " s, " << communicationIntervals_
                  << " exchanges, signal delay=" << delaySteps_ << " s each way; reference="
                  << reference_ << " m" << std::endl;
        std::thread receiver([this] { manager_->start(); });
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        manager_->STC_register(CONTROLLER_ID, DcpState::ALIVE,
            convertToUUID(CONTROLLER_UUID), DcpOpMode::NRT, 1, 0);
        manager_->STC_register(TANK_ID, DcpState::ALIVE,
            convertToUUID(TANK_UUID), DcpOpMode::NRT, 1, 0);

        bool completed = false;
        {
            std::unique_lock<std::mutex> lock(doneMutex_);
            completed = doneCv_.wait_for(lock, std::chrono::seconds(180), [this] { return done_ || failed_; });
        }
        manager_->stop();
        receiver.join();
        if (!completed || failed_) return 1;
        std::cout << "DCP master: stopped cleanly. Results are in outputs/" << std::endl;
        return 0;
    }

private:
    void setSlaveEndpoint(std::uint8_t id, std::uint16_t port) {
        std::uint8_t info[6] = {};
        const std::uint32_t ip = static_cast<std::uint32_t>(asio::ip::address_v4::from_string(HOST).to_ulong());
        std::memcpy(info, &port, sizeof(port));
        std::memcpy(info + 2, &ip, sizeof(ip));
        driver_->getDcpDriver().setSlaveNetworkInformation(id, info);
    }

    void setFloatParameter(std::uint8_t id, std::uint64_t vr, double value) {
        auto* bytes = reinterpret_cast<std::uint8_t*>(&value);
        manager_->CFG_parameter(id, vr, DcpDataType::float64, bytes, sizeof(value));
    }

    void configureController() {
        const auto ip = asio::ip::address_v4::from_string(HOST).to_ulong();
        receivedAcks_[CONTROLLER_ID] = 0;
        manager_->CFG_scope(CONTROLLER_ID, 1, DcpScope::Initialization_Run_NonRealTime);
        manager_->CFG_output(CONTROLLER_ID, 1, 0, CONTROLLER_U_VR);
        manager_->CFG_target_network_information_UDP(CONTROLLER_ID, 1, ip, TANK_DATA_INPUT_PORT);
        manager_->CFG_scope(CONTROLLER_ID, 2, DcpScope::Initialization_Run_NonRealTime);
        manager_->CFG_input(CONTROLLER_ID, 2, 0, CONTROLLER_Y_VR, DcpDataType::float64);
        manager_->CFG_source_network_information_UDP(CONTROLLER_ID, 2, ip, CONTROLLER_DATA_INPUT_PORT);
        manager_->CFG_steps(CONTROLLER_ID, 1, COMMUNICATION_STEP_SECONDS);
        manager_->CFG_time_res(CONTROLLER_ID, 1, 1);
        setFloatParameter(CONTROLLER_ID, CONTROLLER_R_VR, reference_);
        setFloatParameter(CONTROLLER_ID, CONTROLLER_KP_VR, 1.5);
        setFloatParameter(CONTROLLER_ID, CONTROLLER_BIAS_VR, 0.2);
    }

    void configureTank() {
        const auto ip = asio::ip::address_v4::from_string(HOST).to_ulong();
        receivedAcks_[TANK_ID] = 0;
        manager_->CFG_scope(TANK_ID, 1, DcpScope::Initialization_Run_NonRealTime);
        manager_->CFG_input(TANK_ID, 1, 0, TANK_U_VR, DcpDataType::float64);
        manager_->CFG_source_network_information_UDP(TANK_ID, 1, ip, TANK_DATA_INPUT_PORT);
        manager_->CFG_scope(TANK_ID, 2, DcpScope::Initialization_Run_NonRealTime);
        manager_->CFG_output(TANK_ID, 2, 0, TANK_H_VR);
        manager_->CFG_target_network_information_UDP(TANK_ID, 2, ip, CONTROLLER_DATA_INPUT_PORT);
        manager_->CFG_steps(TANK_ID, 2, COMMUNICATION_STEP_SECONDS);
        manager_->CFG_time_res(TANK_ID, 1, 1);
        setFloatParameter(TANK_ID, TANK_AREA_VR, 0.01);
        setFloatParameter(TANK_ID, TANK_K_VR, 0.0005);
        setFloatParameter(TANK_ID, TANK_H0_VR, 0.40);
    }

    void onStateChanged(std::uint8_t sender, DcpState state) {
        if (state == DcpState::ERROR_HANDLING || state == DcpState::ERROR_RESOLVED) {
            fail("A DCP slave entered an error state.");
            return;
        }
        switch (state) {
        case DcpState::CONFIGURATION:
            registered_[sender] = true;
            if (sender == CONTROLLER_ID) configureController();
            else if (sender == TANK_ID) configureTank();
            else fail("Unknown DCP slave ID.");
            break;
        case DcpState::PREPARED:
            manager_->STC_configure(sender, DcpState::PREPARED);
            break;
        case DcpState::CONFIGURED:
            if (!initializeRequested_[sender]) {
                initializeRequested_[sender] = true;
                manager_->STC_initialize(sender, DcpState::CONFIGURED);
            } else if (initialized_[sender] && !initialOutputsSent_[sender]) {
                initialOutputsSent_[sender] = true;
                maybeStartRun();
            }
            break;
        case DcpState::INITIALIZED:
            initialized_[sender] = true;
            manager_->STC_send_outputs(sender, DcpState::INITIALIZED);
            break;
        case DcpState::SYNCHRONIZED:
            synchronized_[sender] = true;
            if (!runStarted_) maybeStartRun();
            else onSynchronized(sender);
            break;
        case DcpState::COMPUTED:
            onComputed(sender);
            break;
        case DcpState::STOPPED:
            manager_->STC_deregister(sender, DcpState::STOPPED);
            break;
        case DcpState::ALIVE:
            if (registered_[sender]) {
                deregistered_[sender] = true;
                if (deregistered_[CONTROLLER_ID] && deregistered_[TANK_ID]) {
                    {
                        std::lock_guard<std::mutex> lock(doneMutex_);
                        done_ = true;
                    }
                    doneCv_.notify_one();
                }
            }
            break;
        default:
            break;
        }
    }

    void maybeStartRun() {
        if (runStarted_ || !initialOutputsSent_[CONTROLLER_ID] || !initialOutputsSent_[TANK_ID]) return;
        runStarted_ = true;
        std::cout << "DCP master: initialized and exchanged initial outputs" << std::endl;
        manager_->STC_run(CONTROLLER_ID, DcpState::CONFIGURED, 0);
        manager_->STC_run(TANK_ID, DcpState::CONFIGURED, 0);
    }

    void onSynchronized(std::uint8_t sender) {
        if (!synchronized_[CONTROLLER_ID] || !synchronized_[TANK_ID]) return;
        if (phase_ == 0) {
            phase_ = 1;
            manager_->STC_do_step(CONTROLLER_ID, DcpState::SYNCHRONIZED, COMMUNICATION_STEP_SECONDS);
        } else if (phase_ == 2 && sender == CONTROLLER_ID) {
            phase_ = 3;
            manager_->STC_do_step(TANK_ID, DcpState::SYNCHRONIZED, COMMUNICATION_STEP_SECONDS);
        } else if (phase_ == 4 && sender == TANK_ID) {
            ++completedSteps_;
            synchronized_[CONTROLLER_ID] = false;
            synchronized_[TANK_ID] = false;
            const auto simulatedSeconds = completedSteps_ * COMMUNICATION_STEP_SECONDS;
            if (completedSteps_ % 10 == 0 || completedSteps_ == communicationIntervals_) {
                std::cout << "DCP master: t=" << simulatedSeconds << "/" << durationSeconds_
                          << " s, exchanges=" << completedSteps_ << "/" << communicationIntervals_ << std::endl;
            }
            if (completedSteps_ >= communicationIntervals_) {
                std::cout << "DCP master: stopping participants" << std::endl;
                manager_->STC_stop(CONTROLLER_ID, DcpState::SYNCHRONIZED);
                manager_->STC_stop(TANK_ID, DcpState::SYNCHRONIZED);
            } else {
                phase_ = 1;
                synchronized_[CONTROLLER_ID] = true;
                synchronized_[TANK_ID] = true;
                manager_->STC_do_step(CONTROLLER_ID, DcpState::SYNCHRONIZED, COMMUNICATION_STEP_SECONDS);
            }
        }
    }

    void onComputed(std::uint8_t sender) {
        if (phase_ == 1 && sender == CONTROLLER_ID) {
            phase_ = 2;
            manager_->STC_send_outputs(CONTROLLER_ID, DcpState::COMPUTED);
        } else if (phase_ == 3 && sender == TANK_ID) {
            phase_ = 4;
            manager_->STC_send_outputs(TANK_ID, DcpState::COMPUTED);
        } else {
            fail("Unexpected DCP COMPUTED state; check step ordering.");
        }
    }

    void fail(const std::string& message) {
        std::cerr << "DCP master: " << message << std::endl;
        {
            std::lock_guard<std::mutex> lock(doneMutex_);
            failed_ = true;
        }
        doneCv_.notify_one();
    }

    std::uint32_t durationSeconds_;
    std::uint32_t delaySteps_;
    std::uint32_t communicationIntervals_;
    double reference_;
    std::unique_ptr<UdpDriver> driver_;
    std::unique_ptr<DcpManagerMaster> manager_;
    std::map<std::uint8_t, std::uint32_t> expectedAcks_;
    std::map<std::uint8_t, std::uint32_t> receivedAcks_;
    std::map<std::uint8_t, bool> registered_;
    std::map<std::uint8_t, bool> initializeRequested_;
    std::map<std::uint8_t, bool> initialized_;
    std::map<std::uint8_t, bool> initialOutputsSent_;
    std::map<std::uint8_t, bool> synchronized_;
    std::map<std::uint8_t, bool> deregistered_;
    std::uint32_t completedSteps_ = 0;
    int phase_ = 0;
    bool runStarted_ = false;
    std::mutex doneMutex_;
    std::condition_variable doneCv_;
    bool done_ = false;
    bool failed_ = false;
};
}

int main(int argc, char** argv) {
    try {
        std::uint32_t durationSeconds = 120;
        std::uint32_t delaySteps = 0;
        double reference = 0.43;
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            if ((arg == "--duration" || arg == "--steps") && i + 1 < argc)
                durationSeconds = static_cast<std::uint32_t>(std::stoul(argv[++i]));
            else if ((arg == "--delay-steps" || arg == "--delay-seconds") && i + 1 < argc)
                delaySteps = static_cast<std::uint32_t>(std::stoul(argv[++i]));
            else if (arg == "--reference" && i + 1 < argc) reference = std::stod(argv[++i]);
            else throw std::runtime_error("Usage: dcp_master [--duration 120] [--delay-seconds 0] [--reference 0.43]");
        }
        if (durationSeconds == 0 || durationSeconds > 120)
            throw std::runtime_error("--duration must be between 1 and 120 seconds.");
        if (delaySteps > 30)
            throw std::runtime_error("--delay-seconds must be between 0 and 30.");
        DcpMasterDemo master(durationSeconds, delaySteps, reference);
        return master.run();
    } catch (const std::exception& ex) {
        std::cerr << "DCP master failed: " << ex.what() << std::endl;
        return 1;
    }
}
