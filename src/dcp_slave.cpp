#include "fmi2_runtime.hpp"

#include <iostream>
#include <cassert>
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
#include <dcp/logic/DcpManagerSlave.hpp>
#include <dcp/driver/ethernet/udp/UdpDriver.hpp>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
constexpr const char* HOST = "127.0.0.1";
constexpr std::uint16_t CONTROLLER_CONTROL_PORT = 55201;
constexpr std::uint16_t TANK_CONTROL_PORT = 55202;

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

constexpr const char* CONTROLLER_UUID = "8e77346c-6274-4f01-86ed-0f077b4d2511";
constexpr const char* TANK_UUID = "1142a519-4294-4b7d-a0e8-80fd4b6b7d1c";

std::shared_ptr<CommonCausality_t> floatInput(double start, const char* unit = "1") {
    auto value = make_CommonCausality_ptr<float64_t>();
    value->Float64->start = std::make_shared<std::vector<float64_t>>(1, start);
    value->Float64->unit = std::make_shared<std::string>(unit);
    return value;
}

std::shared_ptr<Output_t> floatOutput(double start, const char* unit = "1") {
    auto value = make_Output_ptr<float64_t>();
    value->Float64->start = std::make_shared<std::vector<float64_t>>(1, start);
    value->Float64->unit = std::make_shared<std::string>(unit);
    return value;
}

void setCommonMetadata(SlaveDescription_t& d, const std::string& description) {
    d.description = std::make_shared<std::string>(description);
    d.author = std::make_shared<std::string>("DecentraLabs educational example");
    d.version = std::make_shared<std::string>("1.0.0");
    d.generationTool = std::make_shared<std::string>("DecentraLabs local DCP tank demo");
    d.OpMode.NonRealTime = make_NonRealTime_ptr();
    d.TimeRes.resolutions.push_back(make_Resolution());
    d.TimeRes.resolutions.back().numerator = 1;
    d.TimeRes.resolutions.back().denominator = 1;
    d.TimeRes.resolutions.back().fixed = true;
    d.TimeRes.resolutions.back().recommended = std::make_shared<bool>(true);
    d.TransportProtocols.UDP_IPv4 = make_UDP_ptr();
    d.TransportProtocols.UDP_IPv4->DAT_input_output = make_DAT_ptr(HOST);
    d.TransportProtocols.UDP_IPv4->DAT_input_output->availablePortRanges.push_back(
        make_AvailablePortRange(55101, 55102));
    d.TransportProtocols.UDP_IPv4->DAT_parameter = make_DAT_ptr(HOST);
    d.TransportProtocols.UDP_IPv4->DAT_parameter->availablePortRanges.push_back(
        make_AvailablePortRange(55101, 55102));
    d.CapabilityFlags.canAcceptConfigPdus = true;
    d.CapabilityFlags.canHandleReset = true;
    d.CapabilityFlags.canHandleVariableSteps = false;
    d.CapabilityFlags.canMonitorHeartbeat = false;
}

SlaveDescription_t makeDescription(bool controller) {
    if (controller) {
        auto d = make_SlaveDescription(1, 0, "TankLevelController", CONTROLLER_UUID);
        setCommonMetadata(d, "P controller FMU exposed as a DCP NRT slave. The reference is a DCP parameter.");
        d.TransportProtocols.UDP_IPv4->Control = make_Control_ptr(HOST, CONTROLLER_CONTROL_PORT);
        d.Variables.push_back(make_Variable_input("y", CONTROLLER_Y_VR, floatInput(0.40, "m")));
        d.Variables.push_back(make_Variable_output("u", CONTROLLER_U_VR, floatOutput(0.245, "1")));
        d.Variables.push_back(make_Variable_parameter("r", CONTROLLER_R_VR, floatInput(0.43, "m")));
        d.Variables.push_back(make_Variable_parameter("Kp", CONTROLLER_KP_VR, floatInput(1.5)));
        d.Variables.push_back(make_Variable_parameter("uBias", CONTROLLER_BIAS_VR, floatInput(0.2)));
        return d;
    }

    auto d = make_SlaveDescription(1, 0, "TankPlant", TANK_UUID);
    setCommonMetadata(d, "Tank FMI model exposed as a DCP NRT slave. The adapter maps u to qIn using qIn=0.001*u.");
    d.TransportProtocols.UDP_IPv4->Control = make_Control_ptr(HOST, TANK_CONTROL_PORT);
    d.Variables.push_back(make_Variable_input("u", TANK_U_VR, floatInput(0.245)));
    d.Variables.push_back(make_Variable_output("h", TANK_H_VR, floatOutput(0.40, "m")));
    d.Variables.push_back(make_Variable_parameter("A", TANK_AREA_VR, floatInput(0.01, "m2")));
    d.Variables.push_back(make_Variable_parameter("k", TANK_K_VR, floatInput(0.0005, "m2/s")));
    d.Variables.push_back(make_Variable_parameter("h0", TANK_H0_VR, floatInput(0.40, "m")));
    return d;
}

class FmuDcpSlave {
public:
    FmuDcpSlave(bool controller, std::uint32_t delaySteps, std::filesystem::path fmuDir,
                std::filesystem::path csvPath)
        : controller_(controller), delaySteps_(delaySteps), fmuDir_(std::move(fmuDir)), csvPath_(std::move(csvPath)),
          description_(makeDescription(controller_)) {
        udp_ = std::make_unique<UdpDriver>(HOST, controller_ ? CONTROLLER_CONTROL_PORT : TANK_CONTROL_PORT);
        manager_ = std::make_unique<DcpManagerSlave>(description_, udp_->getDcpDriver());
        manager_->setConfigureCallback<SYNC>([this] { configure(); });
        manager_->setInitializeCallback<SYNC>([this] { initialize(); });
        manager_->setSynchronizingNRTStepCallback<SYNC>([this](std::uint64_t steps) { doStep(steps); });
        manager_->setSynchronizedNRTStepCallback<SYNC>([this](std::uint64_t steps) { doStep(steps); });
        manager_->setRunningNRTStepCallback<SYNC>([this](std::uint64_t steps) { doStep(steps); });
        manager_->setTimeResListener<SYNC>([this](std::uint32_t n, std::uint32_t d) {
            timeNumerator_ = n;
            timeDenominator_ = d;
        });
        manager_->setInputOutputUpdateListener<SYNC>([this](std::uint64_t) {
            {
                std::lock_guard<std::mutex> lock(inputMutex_);
                inputUpdated_ = true;
            }
            inputCv_.notify_one();
        });
        manager_->setStateChangedListener<SYNC>([this](DcpState state) {
            const bool firstSynchronization = state == DcpState::SYNCHRONIZED && !synchronizedOnce_;
            if (state != DcpState::COMPUTING && state != DcpState::COMPUTED &&
                state != DcpState::SENDING_D && state != DcpState::SYNCHRONIZED) {
                std::cout << (controller_ ? "[Controller]" : "[Tank]") << " DCP state " << state << std::endl;
            } else if (firstSynchronization) {
                synchronizedOnce_ = true;
                std::cout << (controller_ ? "[Controller]" : "[Tank]") << " DCP state " << state << std::endl;
            }
            if (state == DcpState::CONFIGURATION) registered_ = true;
            if (state == DcpState::ALIVE && registered_) {
                {
                    std::lock_guard<std::mutex> lock(exitMutex_);
                    deregistered_ = true;
                }
                exitCv_.notify_one();
            }
        });
    }

    void run() {
        std::cout << (controller_ ? "[Controller]" : "[Tank]")
                  << " waiting for DCP master on 127.0.0.1:" 
                  << (controller_ ? CONTROLLER_CONTROL_PORT : TANK_CONTROL_PORT) << std::endl;
        std::thread receiver([this] { manager_->start(); });
        {
            std::unique_lock<std::mutex> lock(exitMutex_);
            exitCv_.wait(lock, [this] { return deregistered_; });
        }
        csv_.flush();
        static_cast<AbstractDcpManager*>(manager_.get())->stop();
        receiver.join();
        std::cout << (controller_ ? "[Controller]" : "[Tank]") << " stopped" << std::endl;
    }

private:
    void configure() {
        if (controller_) {
            y_ = manager_->getInput<float64_t*>(CONTROLLER_Y_VR);
            u_ = manager_->getOutput<float64_t*>(CONTROLLER_U_VR);
            r_ = manager_->getParameter<float64_t*>(CONTROLLER_R_VR);
            kp_ = manager_->getParameter<float64_t*>(CONTROLLER_KP_VR);
            bias_ = manager_->getParameter<float64_t*>(CONTROLLER_BIAS_VR);
        } else {
            u_ = manager_->getInput<float64_t*>(TANK_U_VR);
            h_ = manager_->getOutput<float64_t*>(TANK_H_VR);
            area_ = manager_->getParameter<float64_t*>(TANK_AREA_VR);
            k_ = manager_->getParameter<float64_t*>(TANK_K_VR);
            h0_ = manager_->getParameter<float64_t*>(TANK_H0_VR);
        }
        csv_.open(csvPath_);
        if (!csv_) throw std::runtime_error("Cannot write CSV: " + csvPath_.string());
        csv_ << std::setprecision(12);
        if (controller_) csv_ << "time_s,reference_m,level_m,u,flow_m3_s\n";
        else csv_ << "time_s,u,flow_m3_s,level_m\n";
        std::cout << (controller_ ? "[Controller]" : "[Tank]") << " configured" << std::endl;
    }

    void initialize() {
        if (controller_) {
            fmu_ = std::make_unique<fmi2demo::Runtime>(fmuDir_, "PController",
                "{dd60dd62-4273-4d99-a069-9019b4743b53}", 120.0);
            fmu_->instantiate("controller_instance");
            fmu_->setupExperiment();
            fmu_->setReals({3, 4}, {*kp_, *bias_});
            fmu_->setReals({0, 1}, {*r_, *y_});
            fmu_->enterInitializationMode();
            fmu_->exitInitializationMode();
            *u_ = fmu_->getReal(2);
            csv_ << "0," << *r_ << "," << *y_ << "," << *u_ << "," << *u_ * 0.001 << '\n';
        } else {
            fmu_ = std::make_unique<fmi2demo::Runtime>(fmuDir_, "Tank",
                "{8a7f36e7-aeab-4ac5-9691-1d3726b381d5}", 120.0);
            fmu_->instantiate("tank_instance");
            fmu_->setupExperiment();
            fmu_->setReals({2, 3, 4}, {*area_, *k_, *h0_});
            fmu_->setReals({0}, {*u_ * 0.001});
            fmu_->enterInitializationMode();
            fmu_->exitInitializationMode();
            *h_ = fmu_->getReal(1);
            csv_ << "0," << *u_ << "," << *u_ * 0.001 << "," << *h_ << '\n';
        }
        std::cout << (controller_ ? "[Controller]" : "[Tank]") << " FMI model initialized" << std::endl;
    }

    void waitForInput() {
        std::unique_lock<std::mutex> lock(inputMutex_);
        if (!inputCv_.wait_for(lock, std::chrono::seconds(10), [this] { return inputUpdated_; })) {
            throw std::runtime_error("Timed out waiting for the peer DCP data PDU");
        }
        inputUpdated_ = false;
    }

    void doStep(std::uint64_t steps) {
        try {
            waitForInput();
            const double dt = (static_cast<double>(timeNumerator_) / timeDenominator_) * steps;
            if (controller_) {
                const double measuredLevel = delayedInput(*y_, 0.40, delayedFeedback_);
                fmu_->setReals({0, 1}, {*r_, measuredLevel});
                fmu_->doStep(dt);
                *u_ = fmu_->getReal(2);
                const auto t = fmu_->time();
                csv_ << t << ',' << *r_ << ',' << measuredLevel << ',' << *u_ << ',' << *u_ * 0.001 << '\n';
                if (static_cast<int>(t) % 10 == 0)
                    std::cout << "[Controller] t=" << t << " s, y=" << measuredLevel << " m, u=" << *u_ << std::endl;
            } else {
                const double appliedCommand = delayedInput(*u_, 0.245, delayedCommand_);
                const double qIn = appliedCommand * 0.001;
                fmu_->setReals({0}, {qIn});
                fmu_->doStep(dt);
                *h_ = fmu_->getReal(1);
                const auto t = fmu_->time();
                csv_ << t << ',' << appliedCommand << ',' << qIn << ',' << *h_ << '\n';
                if (static_cast<int>(t) % 10 == 0)
                    std::cout << "[Tank] t=" << t << " s, qIn=" << qIn << " m3/s, h=" << *h_ << " m" << std::endl;
            }
        } catch (const std::exception& ex) {
            std::cerr << (controller_ ? "[Controller] " : "[Tank] ") << ex.what() << std::endl;
            std::exit(3);
        }
    }

    double delayedInput(double current, double initial, std::deque<double>& history) {
        if (delaySteps_ == 0) return current;
        if (history.empty()) history.assign(delaySteps_, initial);
        history.push_back(current);
        const double delayed = history.front();
        history.pop_front();
        return delayed;
    }

    bool controller_;
    std::uint32_t delaySteps_;
    std::filesystem::path fmuDir_;
    std::filesystem::path csvPath_;
    SlaveDescription_t description_;
    std::unique_ptr<UdpDriver> udp_;
    std::unique_ptr<DcpManagerSlave> manager_;
    std::unique_ptr<fmi2demo::Runtime> fmu_;
    std::ofstream csv_;
    float64_t* y_ = nullptr;
    float64_t* u_ = nullptr;
    float64_t* h_ = nullptr;
    float64_t* r_ = nullptr;
    float64_t* kp_ = nullptr;
    float64_t* bias_ = nullptr;
    float64_t* area_ = nullptr;
    float64_t* k_ = nullptr;
    float64_t* h0_ = nullptr;
    std::uint32_t timeNumerator_ = 1;
    std::uint32_t timeDenominator_ = 1;
    std::mutex inputMutex_;
    std::condition_variable inputCv_;
    bool inputUpdated_ = false;
    std::deque<double> delayedFeedback_;
    std::deque<double> delayedCommand_;
    std::mutex exitMutex_;
    std::condition_variable exitCv_;
    bool registered_ = false;
    bool deregistered_ = false;
    bool synchronizedOnce_ = false;
};
}

int main(int argc, char** argv) {
    try {
        if (argc != 5 || (std::string(argv[1]) != "controller" && std::string(argv[1]) != "tank")) {
            std::cerr << "Usage: dcp_slave <controller|tank> <delay_seconds> <extracted_fmu_folder> <output_csv>\n";
            return 2;
        }
        const bool controller = std::string(argv[1]) == "controller";
        const auto delaySteps = static_cast<std::uint32_t>(std::stoul(argv[2]));
        if (delaySteps > 30) throw std::runtime_error("delay_seconds must be between 0 and 30.");
        FmuDcpSlave slave(controller, delaySteps, argv[3], argv[4]);
        slave.run();
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "DCP slave failed: " << ex.what() << std::endl;
        return 1;
    }
}
