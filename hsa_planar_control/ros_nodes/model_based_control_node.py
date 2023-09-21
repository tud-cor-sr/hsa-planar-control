from copy import deepcopy
import derivative
from example_interfaces.msg import Float64MultiArray
from functools import partial
from jax import config as jax_config

jax_config.update("jax_enable_x64", True)  # double precision
jax_config.update("jax_platform_name", "cpu")  # use CPU
from jax import Array, jit
from jax import numpy as jnp
import jsrm
from jsrm.parameters.hsa_params import PARAMS_CONTROL
from jsrm.systems import planar_hsa
import rclpy
from rclpy.node import Node
from rclpy.time import Time
from pathlib import Path

from geometry_msgs.msg import Pose2D
from hsa_control_interfaces.msg import PlanarSetpoint
from mocap_optitrack_interfaces.msg import PlanarCsConfiguration

from hsa_actuation.hsa_actuation_base_node import HsaActuationBaseNode
from hsa_planar_control.collocated_form import mapping_into_collocated_form_factory
from hsa_planar_control.controllers.configuration_space_controllers import (
    P_satI_D_plus_steady_state_actuation,
    P_satI_D_collocated_form_plus_steady_state_actuation,
    P_satI_D_collocated_form_plus_gravity_cancellation_elastic_compensation,
)
from hsa_planar_control.controllers.operational_space_controllers import (
    basic_operational_space_pid,
)
from hsa_planar_control.controllers.saturation import saturate_control_inputs


class ModelBasedControlNode(HsaActuationBaseNode):
    def __init__(self):
        super().__init__("model_based_control_node")
        self.declare_parameter("configuration_topic", "configuration")
        self.configuration_sub = self.create_subscription(
            PlanarCsConfiguration,
            self.get_parameter("configuration_topic").value,
            self.configuration_listener_callback,
            10,
        )

        # filepath to symbolic expressions
        sym_exp_filepath = (
            Path(jsrm.__file__).parent
            / "symbolic_expressions"
            / f"planar_hsa_ns-1_nrs-2.dill"
        )
        (
            forward_kinematics_virtual_backbone_fn,
            forward_kinematics_end_effector_fn,
            jacobian_end_effector_fn,
            inverse_kinematics_end_effector_fn,
            dynamical_matrices_fn,
            sys_helpers,
        ) = planar_hsa.factory(sym_exp_filepath)

        self.params = PARAMS_CONTROL.copy()

        # parameter for specifying a different axial rest strain
        self.declare_parameter("sigma_a_eq", self.params["sigma_a_eq"].mean().item())
        sigma_a_eq = self.get_parameter("sigma_a_eq").value
        self.params["sigma_a_eq"] = sigma_a_eq * jnp.ones_like(
            self.params["sigma_a_eq"]
        )
        # actual rest strain
        self.xi_eq = sys_helpers["rest_strains_fn"](self.params)  # rest strains

        self.declare_parameter("phi_max", self.params["phi_max"].mean().item())
        self.params["phi_max"] = self.get_parameter("phi_max").value * jnp.ones_like(
            self.params["phi_max"]
        )

        # initialize state
        self.q = jnp.zeros_like(self.xi_eq)  # generalized coordinates
        self.n_q = self.q.shape[0]  # number of generalized coordinates
        self.q_d = jnp.zeros_like(self.q)  # velocity of generalized coordinates

        # initial actuation coordinates
        phi0 = self.map_motor_angles_to_actuation_coordinates(self.present_motor_angles)

        # pubslishers for actuation coordinates / control inputs
        self.declare_parameter("actuation_coordinates_topic", "actuation_coordinates")
        self.actuation_coordinates_pub = self.create_publisher(
            Float64MultiArray,
            self.get_parameter("actuation_coordinates_topic").value,
            10,
        )
        self.declare_parameter(
            "unsaturated_control_input_topic", "unsaturated_control_input"
        )
        self.unsaturated_control_input_pub = self.create_publisher(
            Float64MultiArray,
            self.get_parameter("unsaturated_control_input_topic").value,
            10,
        )
        self.declare_parameter(
            "saturated_control_input_topic", "saturated_control_input"
        )
        self.saturated_control_input_pub = self.create_publisher(
            Float64MultiArray,
            self.get_parameter("saturated_control_input_topic").value,
            10,
        )

        # history of configurations
        # the longer the history, the more delays we introduce, but the less noise we get
        self.declare_parameter("history_length_for_diff", 16)
        self.t_hs = jnp.zeros((self.get_parameter("history_length_for_diff").value,))
        self.q_hs = jnp.zeros(
            (self.get_parameter("history_length_for_diff").value, self.n_q)
        )

        # method for computing derivative
        self.diff_method = derivative.Spline(s=1.0, k=3)
        self.declare_parameter("configuration_velocity_topic", "configuration_velocity")
        self.configuration_velocity_pub = self.create_publisher(
            Float64MultiArray,
            self.get_parameter("configuration_velocity_topic").value,
            10,
        )

        self.declare_parameter("setpoint_topic", "setpoint")
        self.setpoints_sub = self.create_subscription(
            PlanarSetpoint,
            self.get_parameter("setpoint_topic").value,
            self.setpoint_listener_callback,
            10,
        )
        # self.q_des = jnp.zeros_like(self.q)
        self.q_des = jnp.array(
            [0.0, 0.0, 0.04]
        )  # slight elongation to avoid windup of integral error
        self.chiee_des = jnp.zeros((3,))
        self.phi_ss = jnp.zeros_like(phi0)

        self.setpoint_msg = PlanarSetpoint()
        self.setpoint_msg.q_des.header.stamp = self.get_clock().now().to_msg()
        self.setpoint_msg.q_des.kappa_b = self.q_des[0].item()
        self.setpoint_msg.q_des.sigma_sh = self.q_des[1].item()
        self.setpoint_msg.q_des.sigma_a = self.q_des[2].item()
        self.setpoint_msg.chiee_des.x = self.chiee_des[0].item()
        self.setpoint_msg.chiee_des.y = self.chiee_des[1].item()
        self.setpoint_msg.chiee_des.theta = self.chiee_des[2].item()
        self.setpoint_msg.phi_ss = self.phi_ss.tolist()
        self.setpoint_msg.optimality_error = 0.0

        # re-publishing of setpoints
        self.setpoint_in_control_loop_pub = self.create_publisher(
            PlanarSetpoint, "setpoint_in_control_loop", 10
        )

        self.declare_parameter(
            "controller_type", "P_satI_D_collocated_form_plus_steady_state_actuation"
        )
        self.controller_type = self.get_parameter("controller_type").value
        self.declare_parameter("control_frequency", 25)
        self.control_frequency = self.get_parameter("control_frequency").value
        control_dt = 1 / self.control_frequency
        self.declare_parameter("Kp", 0.0)
        Kp = self.get_parameter("Kp").value * jnp.eye(phi0.shape[0])
        self.declare_parameter("Ki", 0.0)
        Ki = self.get_parameter("Ki").value * jnp.eye(phi0.shape[0])
        self.declare_parameter("Kd", 0.0)
        Kd = self.get_parameter("Kd").value * jnp.eye(phi0.shape[0])
        self.declare_parameter("gamma", 1.0)
        gamma = self.get_parameter("gamma").value * jnp.ones_like(phi0)
        self.controller_state = {
            "integral_error": jnp.zeros_like(phi0),
        }
        map_into_collocated_form_fn, _ = mapping_into_collocated_form_factory(
            sym_exp_filepath, sys_helpers
        )

        if (
            self.controller_type
            == "P_satI_D_collocated_form_plus_steady_state_actuation"
        ):
            self.control_fn = jit(
                partial(
                    P_satI_D_collocated_form_plus_steady_state_actuation,
                    map_into_collocated_form_fn=partial(
                        map_into_collocated_form_fn, self.params
                    ),
                    dt=control_dt,
                    Kp=Kp,
                    Ki=Ki,
                    Kd=Kd,
                    gamma=gamma,
                )
            )
        elif (
            self.controller_type
            == "P_satI_D_collocated_form_plus_gravity_cancellation_elastic_compensation"
        ):
            self.control_fn = jit(
                partial(
                    P_satI_D_collocated_form_plus_gravity_cancellation_elastic_compensation,
                    dynamical_matrices_fn=partial(dynamical_matrices_fn, self.params),
                    map_into_collocated_form_fn=partial(
                        map_into_collocated_form_fn, self.params
                    ),
                    dt=control_dt,
                    Kp=Kp,
                    Ki=Ki,
                    Kd=Kd,
                    gamma=gamma,
                )
            )
        elif self.controller_type == "P_satI_D_plus_steady_state_actuation":
            self.control_fn = jit(
                partial(
                    P_satI_D_plus_steady_state_actuation,
                    dynamical_matrices_fn=partial(dynamical_matrices_fn, self.params),
                    dt=control_dt,
                    Kp=Kp,
                    Ki=Ki,
                    Kd=Kd,
                    gamma=gamma,
                )
            )
        elif self.controller_type == "basic_operational_space_pid":
            self.control_fn = partial(
                basic_operational_space_pid,
                forward_kinematics_end_effector_fn=partial(
                    forward_kinematics_end_effector_fn, self.params
                ),
                jacobian_end_effector_fn=partial(jacobian_end_effector_fn, self.params),
                dt=control_dt,
                Kp=Kp,
                Ki=Ki,
                Kd=Kd,
            )
        else:
            raise NotImplementedError(
                "Controller type {} not implemented".format(self.controller_type)
            )

        phi_dummy = self.map_motor_angles_to_actuation_coordinates(
            self.present_motor_angles
        )
        phi_des_dummy, _ = self.control_fn(
            0.0,
            self.q,
            self.q_d,
            phi_dummy,
            controller_state=self.controller_state,
            pee_des=self.chiee_des[:2],
            q_des=self.q_des,
            phi_ss=self.phi_ss,
        )
        motor_goal_angles_dummy = self.map_actuation_coordinates_to_motor_angles(
            phi_des_dummy
        )

        self.control_timer = self.create_timer(
            1.0 / self.control_frequency, self.call_controller
        )

        self.start_time = self.get_clock().now()

    def configuration_listener_callback(self, msg):
        t = Time.from_msg(msg.header.stamp).nanoseconds / 1e9

        # set the current configuration
        self.q = jnp.array([msg.kappa_b, msg.sigma_sh, msg.sigma_a])

        # update history
        self.t_hs = jnp.roll(self.t_hs, shift=-1, axis=0)
        self.t_hs = self.t_hs.at[-1].set(t)
        self.q_hs = jnp.roll(self.q_hs, shift=-1, axis=0)
        self.q_hs = self.q_hs.at[-1].set(self.q)

    def setpoint_listener_callback(self, msg):
        self.setpoint_msg = msg
        self.q_des = jnp.array(
            [msg.q_des.kappa_b, msg.q_des.sigma_sh, msg.q_des.sigma_a]
        )
        self.chiee_des = jnp.array(
            [msg.chiee_des.x, msg.chiee_des.y, msg.chiee_des.theta]
        )
        self.phi_ss = jnp.array(msg.phi_ss)

    def compute_q_d(self) -> Array:
        """
        Compute the velocity of the generalized coordinates from the history of configurations.
        """
        # if the buffer is not full yet, return the current velocity
        if jnp.any(self.t_hs == 0.0):
            return self.q_d

        # subtract the first time stamp from all time stamps to avoid numerical issues
        t_hs = self.t_hs - self.t_hs[0]

        q_d = jnp.zeros_like(self.q)
        # iterate through configuration variables
        for i in range(self.q_hs.shape[-1]):
            # derivative of all time stamps for configuration variable i
            q_d_hs = self.diff_method.d(self.q_hs[:, i], t_hs)

            q_d = q_d.at[i].set(q_d_hs[-1])

        self.configuration_velocity_pub.publish(Float64MultiArray(data=q_d.tolist()))

        return q_d

    def map_motor_angles_to_actuation_coordinates(self, motor_angles: Array) -> Array:
        """
        Map the motor angles to the actuation coordinates. The actuation coordinates are defined as twist angle
        of an imagined rod on the left and right respectively.
        """
        control_handedness = self.params["h"][
            0
        ]  # handedness of rods in first segment in control model
        phi = jnp.stack(
            [
                (
                    motor_angles[2] * self.rod_handedness[2]
                    + motor_angles[3] * self.rod_handedness[3]
                )
                * control_handedness[0]
                / 2,
                (
                    motor_angles[0] * self.rod_handedness[0]
                    + motor_angles[1] * self.rod_handedness[1]
                )
                * control_handedness[1]
                / 2,
            ]
        )
        return phi

    def map_actuation_coordinates_to_motor_angles(self, phi: Array) -> Array:
        """
        We devise the control input in positive actuation coordinates of shape (2, ). However, we need to actuate
        four motors. This function maps the two actuation coordinates to the four motor angles.
        """
        control_handedness = self.params["h"][
            0
        ]  # handedness of rods in first segment in control model

        motor_angles = jnp.stack(
            [
                phi[1] * control_handedness[1] * self.rod_handedness[0],
                phi[1] * control_handedness[1] * self.rod_handedness[1],
                phi[0] * control_handedness[0] * self.rod_handedness[2],
                phi[0] * control_handedness[0] * self.rod_handedness[3],
            ]
        )
        return motor_angles

    def call_controller(self):
        t = (self.get_clock().now() - self.start_time).nanoseconds / 1e9

        # compute the velocity of the generalized coordinates
        self.q_d = self.compute_q_d()

        # map motor angles to actuation coordinates
        phi = self.map_motor_angles_to_actuation_coordinates(self.present_motor_angles)
        self.actuation_coordinates_pub.publish(Float64MultiArray(data=phi.tolist()))

        # evaluate controller
        phi_des, self.controller_state = self.control_fn(
            t,
            self.q,
            self.q_d,
            phi,
            controller_state=self.controller_state,
            pee_des=self.chiee_des[:2],
            q_des=self.q_des,
            phi_ss=self.phi_ss,
        )

        # self.get_logger().info(f"e_y: {self.controller_state['e_y']}, e_int: {jnp.tanh(self.get_parameter('gamma').value * self.controller_state['e_y'])}")

        # republishing of setpoints
        setpoint_msg = deepcopy(self.setpoint_msg)
        setpoint_msg.q_des.header.stamp = self.get_clock().now().to_msg()
        self.setpoint_in_control_loop_pub.publish(setpoint_msg)

        # compensate for the handedness specified in the parameters
        phi_des = self.params["h"].flatten() * phi_des
        self.unsaturated_control_input_pub.publish(
            Float64MultiArray(data=phi_des.tolist())
        )

        # saturate the control input
        phi_sat, self.controller_state = saturate_control_inputs(
            self.params, phi_des, controller_state=self.controller_state
        )
        self.saturated_control_input_pub.publish(
            Float64MultiArray(data=phi_sat.tolist())
        )

        # self.get_logger().info(f"Saturated control inputs: {phi_sat}")

        # map the actuation coordinates to motor angles
        motor_goal_angles = self.map_actuation_coordinates_to_motor_angles(phi_sat)

        # send motor goal angles to dynamixel motors
        self.set_motor_goal_angles(motor_goal_angles)


def main(args=None):
    rclpy.init(args=args)
    print("Hi from the planar model-based control node.")

    node = ModelBasedControlNode()

    rclpy.spin(node)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
