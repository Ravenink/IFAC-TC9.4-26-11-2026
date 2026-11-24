#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace fmi2demo {

using Status = int;
using Boolean = char;
using ValueReference = std::uint32_t;
using Real = double;
using Component = void*;

using Logger = void (*)(void*, const char*, Status, const char*, const char*, ...);
using AllocateMemory = void* (*)(std::size_t, std::size_t);
using FreeMemory = void (*)(void*);
using StepFinished = void (*)(void*, Status);

struct CallbackFunctions {
    Logger logger;
    AllocateMemory allocateMemory;
    FreeMemory freeMemory;
    StepFinished stepFinished;
    void* componentEnvironment;
};

inline void logger(void*, const char*, Status, const char*, const char*, ...) {}
inline void* allocateMemory(std::size_t count, std::size_t size) { return std::calloc(count, size); }
inline void freeMemory(void* ptr) { std::free(ptr); }
inline void stepFinished(void*, Status) {}

class Runtime {
public:
    Runtime(const std::filesystem::path& extractedFmu, std::string identifier, std::string guid,
            double stopTime)
        : fmuDir_(std::filesystem::absolute(extractedFmu)), identifier_(std::move(identifier)),
          guid_(std::move(guid)), stopTime_(stopTime) {
        if (!std::filesystem::exists(fmuDir_ / "modelDescription.xml"))
            throw std::runtime_error("FMU folder is missing modelDescription.xml: " + fmuDir_.string());
        loadLibrary();
    }

    Runtime(const Runtime&) = delete;
    Runtime& operator=(const Runtime&) = delete;

    ~Runtime() {
        if (component_) {
            if (terminate_) terminate_(component_);
            freeInstance_(component_);
        }
        if (library_) closeLibrary();
    }

    void instantiate(const std::string& instanceName) {
        const auto resourcePath = fmuDir_ / "resources";
        const auto uri = fileUri(resourcePath);
        CallbackFunctions callbacks{logger, allocateMemory, freeMemory, stepFinished, nullptr};
        component_ = instantiate_(instanceName.c_str(), 1, guid_.c_str(), uri.c_str(), &callbacks, 0, 0);
        if (!component_) throw std::runtime_error("fmi2Instantiate failed for " + identifier_);
    }

    void setupExperiment() {
        check(setupExperiment_(component_, 0, 0.0, 0.0, 1, stopTime_), "fmi2SetupExperiment");
    }

    void enterInitializationMode() {
        check(enterInitializationMode_(component_), "fmi2EnterInitializationMode");
    }

    void exitInitializationMode() {
        check(exitInitializationMode_(component_), "fmi2ExitInitializationMode");
    }

    void setReals(const std::vector<ValueReference>& refs, const std::vector<Real>& values) {
        if (refs.size() != values.size()) throw std::runtime_error("FMI value reference/value count mismatch");
        if (refs.empty()) return;
        check(setReal_(component_, refs.data(), refs.size(), values.data()), "fmi2SetReal");
    }

    Real getReal(ValueReference ref) const {
        Real value = 0.0;
        check(getReal_(component_, &ref, 1, &value), "fmi2GetReal");
        return value;
    }

    void doStep(double stepSize) {
        check(doStep_(component_, time_, stepSize, 1), "fmi2DoStep");
        time_ += stepSize;
    }

    double time() const { return time_; }

private:
    using Instantiate = Component (*)(const char*, int, const char*, const char*, const CallbackFunctions*, Boolean, Boolean);
    using SetupExperiment = Status (*)(Component, Boolean, Real, Real, Boolean, Real);
    using EnterInitializationMode = Status (*)(Component);
    using ExitInitializationMode = Status (*)(Component);
    using SetReal = Status (*)(Component, const ValueReference*, std::size_t, const Real*);
    using GetReal = Status (*)(Component, const ValueReference*, std::size_t, Real*);
    using DoStep = Status (*)(Component, Real, Real, Boolean);
    using Terminate = Status (*)(Component);
    using FreeInstance = void (*)(Component);

    template<typename T>
    T symbol(const char* name) {
#ifdef _WIN32
        auto value = reinterpret_cast<T>(GetProcAddress(library_, name));
#else
        auto value = reinterpret_cast<T>(dlsym(library_, name));
#endif
        if (!value) throw std::runtime_error(std::string("FMU is missing FMI function ") + name);
        return value;
    }

    void loadLibrary() {
#ifdef _WIN32
        const auto binary = fmuDir_ / "binaries" / "win64" / (identifier_ + ".dll");
        library_ = LoadLibraryW(binary.wstring().c_str());
        if (!library_) throw std::runtime_error("Could not load FMU binary: " + binary.string());
#else
        const auto binary = fmuDir_ / "binaries" / "linux64" / (identifier_ + ".so");
        library_ = dlopen(binary.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (!library_) throw std::runtime_error("Could not load FMU binary: " + binary.string() + " (" + dlerror() + ")");
#endif
        instantiate_ = symbol<Instantiate>("fmi2Instantiate");
        setupExperiment_ = symbol<SetupExperiment>("fmi2SetupExperiment");
        enterInitializationMode_ = symbol<EnterInitializationMode>("fmi2EnterInitializationMode");
        exitInitializationMode_ = symbol<ExitInitializationMode>("fmi2ExitInitializationMode");
        setReal_ = symbol<SetReal>("fmi2SetReal");
        getReal_ = symbol<GetReal>("fmi2GetReal");
        doStep_ = symbol<DoStep>("fmi2DoStep");
        terminate_ = symbol<Terminate>("fmi2Terminate");
        freeInstance_ = symbol<FreeInstance>("fmi2FreeInstance");
    }

    std::string fileUri(const std::filesystem::path& path) const {
        std::string raw = std::filesystem::absolute(path).generic_string();
#ifdef _WIN32
        if (raw.size() >= 2 && raw[1] == ':') raw = "/" + raw;
#endif
        std::string uri = "file://" + raw;
        std::string encoded;
        for (unsigned char c : uri) {
            if (c == ' ') encoded += "%20";
            else if (c == '#') encoded += "%23";
            else if (c == '%') encoded += "%25";
            else encoded += static_cast<char>(c);
        }
        return encoded;
    }

    void check(Status status, const char* operation) const {
        if (status > 1) throw std::runtime_error(std::string(operation) + " returned FMI status " + std::to_string(status));
    }

    void closeLibrary() {
#ifdef _WIN32
        FreeLibrary(library_);
#else
        dlclose(library_);
#endif
        library_ = nullptr;
    }

    std::filesystem::path fmuDir_;
    std::string identifier_;
    std::string guid_;
    double stopTime_;
    double time_ = 0.0;
    Component component_ = nullptr;
#ifdef _WIN32
    HMODULE library_ = nullptr;
#else
    void* library_ = nullptr;
#endif
    Instantiate instantiate_ = nullptr;
    SetupExperiment setupExperiment_ = nullptr;
    EnterInitializationMode enterInitializationMode_ = nullptr;
    ExitInitializationMode exitInitializationMode_ = nullptr;
    SetReal setReal_ = nullptr;
    GetReal getReal_ = nullptr;
    DoStep doStep_ = nullptr;
    Terminate terminate_ = nullptr;
    FreeInstance freeInstance_ = nullptr;
};

} // namespace fmi2demo
