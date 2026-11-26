#include <stddef.h>
#include <stdint.h>

#if defined(PE_CROSS)
#  define FMI_API __attribute__((ms_abi))
#else
#  define FMI_API
#endif

#define FMI_OK 0
#define FMI_ERROR 3
#define MAX_INSTANCES 16

typedef struct {
    int active;
    int has_previous_error;
    double r;
    double y;
    double kp;
    double ki;
    double u_bias;
    double integral_error;
    double previous_error;
} PIController;

static PIController instances[MAX_INSTANCES];

static inline PIController *get_controller(void *component)
{
    PIController *controller = (PIController *)component;
    if (controller == NULL || !controller->active) return NULL;
    return controller;
}

static inline double calculate_output(const PIController *controller)
{
    const double error = controller->r - controller->y;
    return controller->u_bias + controller->kp * error
           + controller->ki * controller->integral_error;
}

FMI_API const char *fmi2GetTypesPlatform(void)
{
    return "default";
}

FMI_API const char *fmi2GetVersion(void)
{
    return "2.0";
}

FMI_API int fmi2SetDebugLogging(void *component, int logging_on,
                                size_t n_categories, const char *categories[])
{
    (void)component; (void)logging_on; (void)n_categories; (void)categories;
    return FMI_OK;
}

FMI_API void *fmi2Instantiate(const char *instance_name, int fmu_type,
                              const char *guid, const char *resource_location,
                              const void *callbacks, int visible, int logging_on)
{
    (void)instance_name; (void)guid; (void)resource_location; (void)callbacks;
    (void)visible; (void)logging_on;
    if (fmu_type != 1) return NULL;

    for (size_t i = 0; i < MAX_INSTANCES; ++i) {
        if (!instances[i].active) {
            PIController *controller = &instances[i];
            controller->active = 1;
            controller->has_previous_error = 0;
            controller->r = 0.0;
            controller->y = 0.0;
            controller->kp = 1.5;
            controller->ki = 0.05;
            controller->u_bias = 0.2;
            controller->integral_error = 0.0;
            controller->previous_error = 0.0;
            return controller;
        }
    }
    return NULL;
}

FMI_API void fmi2FreeInstance(void *component)
{
    PIController *controller = (PIController *)component;
    if (controller != NULL) controller->active = 0;
}

FMI_API int fmi2SetupExperiment(void *component, int tolerance_defined,
                                double tolerance, double start_time,
                                int stop_time_defined, double stop_time)
{
    (void)tolerance_defined; (void)tolerance; (void)start_time;
    (void)stop_time_defined; (void)stop_time;
    return get_controller(component) != NULL ? FMI_OK : FMI_ERROR;
}

FMI_API int fmi2EnterInitializationMode(void *component)
{
    PIController *controller = get_controller(component);
    if (controller == NULL) return FMI_ERROR;
    controller->integral_error = 0.0;
    controller->previous_error = 0.0;
    controller->has_previous_error = 0;
    return FMI_OK;
}

FMI_API int fmi2ExitInitializationMode(void *component)
{
    return get_controller(component) != NULL ? FMI_OK : FMI_ERROR;
}

FMI_API int fmi2Terminate(void *component)
{
    return get_controller(component) != NULL ? FMI_OK : FMI_ERROR;
}

FMI_API int fmi2Reset(void *component)
{
    PIController *controller = get_controller(component);
    if (controller == NULL) return FMI_ERROR;
    controller->has_previous_error = 0;
    controller->r = 0.0;
    controller->y = 0.0;
    controller->kp = 1.5;
    controller->ki = 0.05;
    controller->u_bias = 0.2;
    controller->integral_error = 0.0;
    controller->previous_error = 0.0;
    return FMI_OK;
}

FMI_API int fmi2GetReal(void *component, const uint32_t value_references[],
                        size_t n_value_references, double values[])
{
    PIController *controller = get_controller(component);
    if (controller == NULL) return FMI_ERROR;
    for (size_t i = 0; i < n_value_references; ++i) {
        switch (value_references[i]) {
            case 0: values[i] = controller->r; break;
            case 1: values[i] = controller->y; break;
            case 2: values[i] = calculate_output(controller); break;
            case 3: values[i] = controller->kp; break;
            case 4: values[i] = controller->ki; break;
            case 5: values[i] = controller->u_bias; break;
            default: return FMI_ERROR;
        }
    }
    return FMI_OK;
}

FMI_API int fmi2SetReal(void *component, const uint32_t value_references[],
                        size_t n_value_references, const double values[])
{
    PIController *controller = get_controller(component);
    if (controller == NULL) return FMI_ERROR;
    for (size_t i = 0; i < n_value_references; ++i) {
        switch (value_references[i]) {
            case 0: controller->r = values[i]; break;
            case 1: controller->y = values[i]; break;
            case 3: controller->kp = values[i]; break;
            case 4: controller->ki = values[i]; break;
            case 5: controller->u_bias = values[i]; break;
            default: return FMI_ERROR;
        }
    }
    return FMI_OK;
}

FMI_API int fmi2DoStep(void *component, double current_communication_point,
                       double communication_step_size,
                       int no_set_fmu_state_prior_to_current_point)
{
    (void)current_communication_point;
    (void)no_set_fmu_state_prior_to_current_point;
    PIController *controller = get_controller(component);
    if (controller == NULL || communication_step_size <= 0.0) return FMI_ERROR;

    const double error = controller->r - controller->y;
    if (controller->has_previous_error) {
        controller->integral_error += 0.5 * (controller->previous_error + error)
                                      * communication_step_size;
    } else {
        controller->integral_error += error * communication_step_size;
        controller->has_previous_error = 1;
    }
    controller->previous_error = error;
    return FMI_OK;
}

/* Unsupported FMI services. They are exported so FMI importers can resolve the
   complete FMI 2.0 Co-Simulation function table; this FMU advertises no support
   for these optional services. */
FMI_API int fmi2GetInteger(void *c, const uint32_t vr[], size_t n, int v[])
{ (void)c; (void)vr; (void)n; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetBoolean(void *c, const uint32_t vr[], size_t n, int v[])
{ (void)c; (void)vr; (void)n; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetString(void *c, const uint32_t vr[], size_t n, const char *v[])
{ (void)c; (void)vr; (void)n; (void)v; return FMI_ERROR; }
FMI_API int fmi2SetInteger(void *c, const uint32_t vr[], size_t n, const int v[])
{ (void)c; (void)vr; (void)n; (void)v; return FMI_ERROR; }
FMI_API int fmi2SetBoolean(void *c, const uint32_t vr[], size_t n, const int v[])
{ (void)c; (void)vr; (void)n; (void)v; return FMI_ERROR; }
FMI_API int fmi2SetString(void *c, const uint32_t vr[], size_t n, const char *v[])
{ (void)c; (void)vr; (void)n; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetFMUstate(void *c, void **s) { (void)c; (void)s; return FMI_ERROR; }
FMI_API int fmi2SetFMUstate(void *c, void *s) { (void)c; (void)s; return FMI_ERROR; }
FMI_API int fmi2FreeFMUstate(void *c, void **s) { (void)c; (void)s; return FMI_ERROR; }
FMI_API int fmi2SerializedFMUstateSize(void *c, void *s, size_t *n)
{ (void)c; (void)s; (void)n; return FMI_ERROR; }
FMI_API int fmi2SerializeFMUstate(void *c, void *s, char bytes[], size_t n)
{ (void)c; (void)s; (void)bytes; (void)n; return FMI_ERROR; }
FMI_API int fmi2DeSerializeFMUstate(void *c, const char bytes[], size_t n, void **s)
{ (void)c; (void)bytes; (void)n; (void)s; return FMI_ERROR; }
FMI_API int fmi2GetDirectionalDerivative(void *c, const uint32_t u[], size_t nu,
                                          const uint32_t k[], size_t nk,
                                          const double dk[], double du[])
{ (void)c; (void)u; (void)nu; (void)k; (void)nk; (void)dk; (void)du; return FMI_ERROR; }
FMI_API int fmi2SetRealInputDerivatives(void *c, const uint32_t vr[], size_t n,
                                        const int order[], const double value[])
{ (void)c; (void)vr; (void)n; (void)order; (void)value; return FMI_ERROR; }
FMI_API int fmi2GetRealOutputDerivatives(void *c, const uint32_t vr[], size_t n,
                                         const int order[], double value[])
{ (void)c; (void)vr; (void)n; (void)order; (void)value; return FMI_ERROR; }
FMI_API int fmi2CancelStep(void *c) { (void)c; return FMI_ERROR; }
FMI_API int fmi2GetStatus(void *c, int k, int *v) { (void)c; (void)k; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetRealStatus(void *c, int k, double *v) { (void)c; (void)k; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetIntegerStatus(void *c, int k, int *v) { (void)c; (void)k; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetBooleanStatus(void *c, int k, int *v) { (void)c; (void)k; (void)v; return FMI_ERROR; }
FMI_API int fmi2GetStringStatus(void *c, int k, const char **v) { (void)c; (void)k; (void)v; return FMI_ERROR; }
