import qimpy as qp
from _advect import Advect
import torch


def main():
    import matplotlib.pyplot as plt

    qp.utils.log_config()
    qp.rc.init()
    assert qp.rc.n_procs == 1  # MPI not yet supported

    x_y_bottom_left = [0, 0]
    x_y_bottom_right = [1.0, 0.0]
    x_y_top_right = [1.5, 1.0]
    x_y_top_left = [0.5, 1.0]

    x_y_corners = [x_y_bottom_left, x_y_bottom_right, x_y_top_right, x_y_top_left]
    sim = Advect(
        x_y_corners,
        reflect_boundaries=False,
        contact_width=0.0,
        v_F=200.0,
        Nx=128,
        Ny=160,
    )
    sigma = 0.05
    sim.rho[:, :, 0] = torch.exp(
        -((sim.q[:, :, 0] - sim.Lx / 2) ** 2 + (sim.q[:, :, 1] - sim.Ly / 2) ** 2)
        / sigma**2
    ).detach()
    for time_step in range(256):
        qp.log.info(f"{time_step = }")
        plt.clf()
        sim.plot_streamlines(plt, dict(levels=100), dict(linewidth=1.0))
        plt.gca().set_aspect("equal")
        plt.savefig(
            f"advect_animation/blob_advect_{time_step:04d}.png",
            bbox_inches="tight",
            dpi=200,
        )
        sim.time_step()

    # Plot only at end (for easier performance benchmarking of time steps):
    qp.log.info("Plotting density and streamlines")

    qp.utils.StopWatch.print_stats()


if __name__ == "__main__":
    main()
