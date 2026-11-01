//! Experimental durable Factory slice. Python remains the production controller.
mod config;
pub mod contracts;
mod error;
mod store;
mod worker;
pub mod workflow;

pub use config::Config;
pub use contracts::{Interaction, Operation, OperationState, Run, StartRequest, Status};
pub use error::{Error, Result};
pub use store::{FactoryStore, SqliteFactoryStore};
pub use worker::{CrashPoint, FakeWorker, Faults, NoFaults, Reconciliation, Worker};

mod graph;
mod runtime;
pub use runtime::{Runtime, Services};

pub mod control;
pub mod http;
pub mod planning;
